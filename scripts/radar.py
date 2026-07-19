#!/usr/bin/env python3
"""Collect GitHub repositories, persist weekly snapshots, and build two rankings."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2022-11-28"
USER_AGENT = "github-weekly-radar/1.0"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class GitHubClient:
    def __init__(self, token: str, timeout: int = 30) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self.timeout = timeout
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"https://api.github.com{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        for attempt in range(3):
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                retryable = error.code in {403, 429, 500, 502, 503, 504}
                if not retryable or attempt == 2:
                    body = error.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"GitHub API {error.code} for {url}: {body[:300]}") from error
                wait_seconds = 2 ** attempt * 5
                reset = error.headers.get("X-RateLimit-Reset")
                if reset:
                    wait_seconds = max(wait_seconds, min(60, int(reset) - int(time.time()) + 1))
                log(f"GitHub API retry in {wait_seconds}s: {url}")
                time.sleep(wait_seconds)
            except urllib.error.URLError as error:
                if attempt == 2:
                    raise RuntimeError(f"Network error for {url}: {error}") from error
                time.sleep(2 ** attempt * 3)
        raise AssertionError("unreachable")

    def search_repositories(
        self,
        query: str,
        *,
        sort: str = "stars",
        per_page: int = 60,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "/search/repositories",
            {"q": query, "sort": sort, "order": "desc", "per_page": per_page, "page": 1},
        )
        return payload.get("items", [])

    def get_repository(self, full_name: str) -> dict[str, Any]:
        owner, repo = full_name.split("/", 1)
        return self._request_json(f"/repos/{owner}/{repo}")

    def has_readme(self, full_name: str) -> bool:
        owner, repo = full_name.split("/", 1)
        try:
            self._request_json(f"/repos/{owner}/{repo}/readme")
            return True
        except RuntimeError as error:
            if "GitHub API 404" in str(error):
                return False
            raise

    def weekly_trending_names(self) -> list[str]:
        request = urllib.request.Request(
            "https://github.com/trending?since=weekly",
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                html = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            log(f"GitHub Trending unavailable; continuing without it: {error}")
            return []

        names = re.findall(
            r'<h2[^>]*>.*?<a[^>]+href="/([^"/\s]+/[^"/\s]+)"',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return list(dict.fromkeys(names))[:30]


def normalize_repository(raw: dict[str, Any]) -> dict[str, Any]:
    license_payload = raw.get("license") or {}
    owner_payload = raw.get("owner") or {}
    return {
        "full_name": raw.get("full_name", ""),
        "name": raw.get("name", ""),
        "owner": owner_payload.get("login", ""),
        "html_url": raw.get("html_url", ""),
        "description": (raw.get("description") or "").strip(),
        "language": raw.get("language") or "Unknown",
        "topics": sorted(set(raw.get("topics") or [])),
        "license": license_payload.get("spdx_id") or "",
        "stars": int(raw.get("stargazers_count") or 0),
        "forks": int(raw.get("forks_count") or 0),
        "open_issues": int(raw.get("open_issues_count") or 0),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "pushed_at": raw.get("pushed_at"),
        "fork": bool(raw.get("fork")),
        "archived": bool(raw.get("archived")),
        "disabled": bool(raw.get("disabled")),
        "matched_categories": [],
        "sources": [],
    }


def merge_repository(
    repositories: dict[str, dict[str, Any]],
    raw: dict[str, Any],
    *,
    category: str | None = None,
    source: str,
) -> None:
    normalized = normalize_repository(raw)
    full_name = normalized["full_name"]
    if not full_name:
        return
    existing = repositories.get(full_name)
    if existing:
        categories = set(existing.get("matched_categories", []))
        sources = set(existing.get("sources", []))
        existing.update(normalized)
        existing["matched_categories"] = sorted(categories)
        existing["sources"] = sorted(sources)
        target = existing
    else:
        repositories[full_name] = normalized
        target = normalized
    if category and category not in target["matched_categories"]:
        target["matched_categories"].append(category)
        target["matched_categories"].sort()
    if source not in target["sources"]:
        target["sources"].append(source)
        target["sources"].sort()


def classify_repository(repo: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    text = " ".join(
        [repo.get("name", ""), repo.get("description", ""), *repo.get("topics", [])]
    ).lower()
    topic_set = {topic.lower() for topic in repo.get("topics", [])}
    discovered = set(repo.get("matched_categories", []))
    priority = {"productivity_dev": 1, "ai_agent": 2, "medical_digital": 3}
    scores: dict[str, int] = {}

    for key, category in config["categories"].items():
        score = 2 if key in discovered else 0
        score += sum(3 for topic in category.get("topics", []) if topic.lower() in topic_set)
        score += sum(1 for keyword in category.get("keywords", []) if keyword.lower() in text)
        scores[key] = score

    key = max(scores, key=lambda item: (scores[item], priority.get(item, 0)))
    return key, config["categories"][key]["label"]


def is_eligible(repo: dict[str, Any], config: dict[str, Any], now: dt.datetime) -> bool:
    if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
        return False
    if not repo.get("license") or repo.get("license") == "NOASSERTION":
        return False

    pushed_at = parse_timestamp(repo.get("pushed_at"))
    if not pushed_at:
        return False
    active_cutoff = now - dt.timedelta(days=int(config["active_within_days"]))
    if pushed_at < active_cutoff:
        return False

    name = repo.get("name", "").lower()
    if any(name.startswith(prefix.lower()) for prefix in config.get("excluded_name_prefixes", [])):
        return False

    topics = {topic.lower() for topic in repo.get("topics", [])}
    excluded_topics = {topic.lower() for topic in config.get("excluded_topics", [])}
    if topics & excluded_topics:
        return False

    description = repo.get("description", "").lower()
    if any(phrase.lower() in description for phrase in config.get("excluded_description_phrases", [])):
        return False
    return True


def age_days(repo: dict[str, Any], now: dt.datetime) -> float:
    created_at = parse_timestamp(repo.get("created_at"))
    if not created_at:
        return 999999.0
    seconds = max(86400.0, (now - created_at).total_seconds())
    return seconds / 86400.0


def trim_candidates(
    repositories: Iterable[dict[str, Any]], config: dict[str, Any], now: dt.datetime
) -> list[dict[str, Any]]:
    limit = int(config["candidate_limit"])

    def discovery_score(repo: dict[str, Any]) -> tuple[float, int]:
        stars = int(repo.get("stars", 0))
        daily = stars / age_days(repo, now)
        trending_bonus = 1_000_000 if "github_trending_weekly" in repo.get("sources", []) else 0
        return (trending_bonus + max(stars, daily * 120), stars)

    return sorted(repositories, key=discovery_score, reverse=True)[:limit]


def previous_snapshot_path(snapshot_dir: Path, current_date: dt.date) -> Path | None:
    candidates = [
        path
        for path in snapshot_dir.glob("????-??-??.json")
        if path.stem < current_date.isoformat()
    ]
    return max(candidates, key=lambda path: path.stem) if candidates else None


def previous_repository_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not snapshot:
        return {}
    return {repo["full_name"]: repo for repo in snapshot.get("repositories", [])}


def enrich_ranking_metrics(
    repositories: Iterable[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for original in repositories:
        repo = dict(original)
        old = previous.get(repo["full_name"])
        if old:
            old_stars = int(old.get("stars", 0))
            delta = max(0, int(repo.get("stars", 0)) - old_stars)
            growth_rate = delta / old_stars if old_stars else None
        else:
            old_stars = None
            delta = None
            growth_rate = None
        repo["previous_stars"] = old_stars
        repo["weekly_star_delta"] = delta
        repo["weekly_growth_rate"] = growth_rate
        repo["daily_star_average"] = int(repo.get("stars", 0)) / age_days(repo, now)
        enriched.append(repo)
    return enriched


def rank_repositories(
    repositories: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    top_n = int(config["top_n"])
    recent_cutoff = now - dt.timedelta(days=int(config["lookback_days"]))
    enriched = enrich_ranking_metrics(repositories, previous, now)

    recent = [
        repo
        for repo in enriched
        if (parse_timestamp(repo.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        >= recent_cutoff
    ]
    recent.sort(key=lambda repo: (int(repo.get("stars", 0)), repo["full_name"]), reverse=True)

    if previous:
        hot = [repo for repo in enriched if repo.get("weekly_star_delta") is not None]
        hot.sort(
            key=lambda repo: (
                int(repo.get("weekly_star_delta") or 0),
                float(repo.get("weekly_growth_rate") or 0),
                int(repo.get("stars", 0)),
            ),
            reverse=True,
        )
        mode = "weekly_delta"
    else:
        cold_start_cutoff = now - dt.timedelta(days=90)
        hot = [
            repo
            for repo in enriched
            if (parse_timestamp(repo.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
            >= cold_start_cutoff
        ]
        hot.sort(
            key=lambda repo: (float(repo.get("daily_star_average") or 0), int(repo.get("stars", 0))),
            reverse=True,
        )
        mode = "cold_start_daily_average"
    return recent[: top_n * 4], hot[: top_n * 4], mode


def select_with_readme(
    client: GitHubClient,
    ranked: list[dict[str, Any]],
    top_n: int,
    readme_cache: dict[str, bool],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for repo in ranked:
        full_name = repo["full_name"]
        if full_name not in readme_cache:
            try:
                readme_cache[full_name] = client.has_readme(full_name)
            except RuntimeError as error:
                log(f"README check failed for {full_name}; excluding it: {error}")
                readme_cache[full_name] = False
        if readme_cache[full_name]:
            selected.append(repo)
        if len(selected) == top_n:
            break
    return selected


def prepare_public_entry(repo: dict[str, Any], rank: int, double_ranked: bool) -> dict[str, Any]:
    return {
        "rank": rank,
        "full_name": repo["full_name"],
        "name": repo["name"],
        "url": repo["html_url"],
        "category_key": repo["category_key"],
        "category": repo["category"],
        "description": repo["description"],
        "language": repo["language"],
        "license": repo["license"],
        "topics": repo["topics"],
        "stars": repo["stars"],
        "previous_stars": repo.get("previous_stars"),
        "weekly_star_delta": repo.get("weekly_star_delta"),
        "weekly_growth_rate": repo.get("weekly_growth_rate"),
        "daily_star_average": round(float(repo.get("daily_star_average") or 0), 2),
        "forks": repo["forks"],
        "open_issues": repo["open_issues"],
        "created_at": repo["created_at"],
        "updated_at": repo["updated_at"],
        "pushed_at": repo["pushed_at"],
        "sources": repo["sources"],
        "double_ranked": double_ranked,
    }


def markdown_escape(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def compact_number(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def render_table(entries: list[dict[str, Any]], mode: str) -> list[str]:
    if mode == "new":
        lines = [
            "| # | 项目 | 赛道 | 简介 | 语言 | License | Star |",
            "|---:|---|---|---|---|---|---:|",
        ]
        for item in entries:
            badge = " ⭐双榜" if item["double_ranked"] else ""
            project = f"[{item['full_name']}]({item['url']}){badge}"
            lines.append(
                "| {rank} | {project} | {category} | {description} | {language} | {license} | {stars} |".format(
                    rank=item["rank"],
                    project=project,
                    category=markdown_escape(item["category"]),
                    description=markdown_escape(item["description"] or "—"),
                    language=markdown_escape(item["language"]),
                    license=markdown_escape(item["license"]),
                    stars=compact_number(item["stars"]),
                )
            )
        return lines

    lines = [
        "| # | 项目 | 赛道 | 总 Star | 7天新增 | 增长率 | 日均 Star |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for item in entries:
        badge = " ⭐双榜" if item["double_ranked"] else ""
        project = f"[{item['full_name']}]({item['url']}){badge}"
        rate = item.get("weekly_growth_rate")
        lines.append(
            "| {rank} | {project} | {category} | {stars} | {delta} | {rate} | {daily:.1f} |".format(
                rank=item["rank"],
                project=project,
                category=markdown_escape(item["category"]),
                stars=compact_number(item["stars"]),
                delta=compact_number(item.get("weekly_star_delta")),
                rate=f"{rate:.1%}" if rate is not None else "—",
                daily=float(item.get("daily_star_average") or 0),
            )
        )
    return lines


def render_report(payload: dict[str, Any]) -> str:
    stats = payload["stats"]
    mode = payload["ranking_mode"]
    lines = [
        f"# GitHub 开源项目雷达 · {payload['date']}",
        "",
        f"> 候选项目 {stats['candidate_count']} 个；近30天新项目榜 {stats['new_list_count']} 个；增长榜 {stats['growth_list_count']} 个。",
        "",
        "## 近30天新项目 Star Top 10",
        "",
        *render_table(payload["lists"]["new_projects_top_stars"], "new"),
        "",
        "## 7天增长 Top 10" if mode == "weekly_delta" else "## 首周增长观察榜 Top 10",
        "",
    ]
    if mode == "cold_start_daily_average":
        lines.extend(
            [
                "> 首周尚无上周快照，本榜按项目创建以来的日均 Star 排名，并非真实7天增量；第二周起自动切换为真实周增量。",
                "",
            ]
        )
    lines.extend(render_table(payload["lists"]["weekly_growth"], "growth"))
    lines.extend(
        [
            "",
            "## 口径说明",
            "",
            "- 范围：AI与智能体、医疗数智化、效率与开发工具，三个赛道合并排名。",
            "- 新项目榜：最近30天创建且通过质量筛选的仓库，按当前 Star 总数排序。",
            "- 增长榜：本周 Star 减去上周快照 Star，以绝对增量为主、增长率为辅。",
            "- 准入：非 Fork、非归档、有开源许可证和 README、最近90天有提交。",
            "- 排除：纯 Awesome/资料清单、课程作业、镜像、纯数据集与纯模型权重等。",
            "- 重复项目保留原始名次，并标记“⭐双榜”。",
            "",
            f"生成时间：{payload['generated_at']}  ",
            f"对比快照：{payload['period']['previous_snapshot_date'] or '无（首周基线）'}",
            "",
        ]
    )
    return "\n".join(lines)


def collect_candidates(
    client: GitHubClient,
    config: dict[str, Any],
    now: dt.datetime,
    previous: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    repositories: dict[str, dict[str, Any]] = {}
    recent_date = (now - dt.timedelta(days=int(config["lookback_days"]))).date().isoformat()
    active_date = (now - dt.timedelta(days=int(config["active_within_days"]))).date().isoformat()
    per_page = int(config["search_results_per_query"])
    delay = float(config.get("search_delay_seconds", 2.1))

    for category_key, category in config["categories"].items():
        for topic in category["topics"]:
            queries = [
                (
                    f"topic:{topic} created:>={recent_date} fork:false archived:false",
                    f"topic:{topic}:recent",
                ),
                (
                    f"topic:{topic} stars:>={config['minimum_stars_for_active_search']} "
                    f"pushed:>={active_date} fork:false archived:false",
                    f"topic:{topic}:active",
                ),
            ]
            for query, source in queries:
                log(f"Searching {source}")
                try:
                    results = client.search_repositories(query, sort="stars", per_page=per_page)
                except RuntimeError as error:
                    log(f"Search failed and was skipped: {error}")
                    results = []
                for raw in results:
                    merge_repository(repositories, raw, category=category_key, source=source)
                if delay:
                    time.sleep(delay)

    if config.get("include_github_trending", True):
        for full_name in client.weekly_trending_names():
            try:
                raw = client.get_repository(full_name)
            except RuntimeError as error:
                log(f"Trending repository skipped ({full_name}): {error}")
                continue
            merge_repository(repositories, raw, source="github_trending_weekly")

    # Keep measuring last week's pool even when a repository drops out of this week's searches.
    for full_name, old_repo in previous.items():
        if full_name in repositories:
            continue
        try:
            raw = client.get_repository(full_name)
            merge_repository(
                repositories,
                raw,
                category=old_repo.get("category_key"),
                source="previous_snapshot",
            )
        except RuntimeError as error:
            log(f"Previous candidate skipped ({full_name}): {error}")

    eligible: list[dict[str, Any]] = []
    for repo in repositories.values():
        category_key, category_label = classify_repository(repo, config)
        repo["category_key"] = category_key
        repo["category"] = category_label
        if is_eligible(repo, config, now):
            eligible.append(repo)
    return trim_candidates(eligible, config, now)


def build_radar(
    client: GitHubClient,
    config: dict[str, Any],
    output_root: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    snapshot_dir = output_root / "data" / "snapshots"
    current_date = now.date()
    prior_path = previous_snapshot_path(snapshot_dir, current_date)
    prior_snapshot = load_json(prior_path) if prior_path else None
    previous = previous_repository_map(prior_snapshot)

    candidates = collect_candidates(client, config, now, previous)
    preliminary_new, preliminary_hot, ranking_mode = rank_repositories(
        candidates, previous, config, now
    )
    readme_cache: dict[str, bool] = {}
    top_n = int(config["top_n"])
    top_new = select_with_readme(client, preliminary_new, top_n, readme_cache)
    top_hot = select_with_readme(client, preliminary_hot, top_n, readme_cache)

    new_names = {repo["full_name"] for repo in top_new}
    hot_names = {repo["full_name"] for repo in top_hot}
    duplicates = new_names & hot_names

    new_entries = [
        prepare_public_entry(repo, rank, repo["full_name"] in duplicates)
        for rank, repo in enumerate(top_new, 1)
    ]
    hot_entries = [
        prepare_public_entry(repo, rank, repo["full_name"] in duplicates)
        for rank, repo in enumerate(top_hot, 1)
    ]

    snapshot = {
        "date": current_date.isoformat(),
        "generated_at": iso_z(now),
        "candidate_count": len(candidates),
        "repositories": candidates,
    }
    snapshot_path = snapshot_dir / f"{current_date.isoformat()}.json"
    write_json(snapshot_path, snapshot)

    payload = {
        "schema_version": 1,
        "date": current_date.isoformat(),
        "generated_at": iso_z(now),
        "ranking_mode": ranking_mode,
        "period": {
            "lookback_days": int(config["lookback_days"]),
            "growth_period_days": (
                (current_date - dt.date.fromisoformat(prior_path.stem)).days if prior_path else None
            ),
            "previous_snapshot_date": prior_path.stem if prior_path else None,
        },
        "categories": [category["label"] for category in config["categories"].values()],
        "stats": {
            "candidate_count": len(candidates),
            "new_list_count": len(new_entries),
            "growth_list_count": len(hot_entries),
            "double_ranked_count": len(duplicates),
        },
        "lists": {
            "new_projects_top_stars": new_entries,
            "weekly_growth": hot_entries,
        },
    }
    write_json(output_root / "data" / "latest.json", payload)
    report = render_report(payload)
    write_text(output_root / "reports" / f"{current_date.isoformat()}.md", report)
    write_text(output_root / "reports" / "latest.md", report)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "radar.json")
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument(
        "--date",
        help="Override UTC run date for controlled tests, in YYYY-MM-DD format.",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    now = dt.datetime.now(dt.timezone.utc)
    if args.date:
        now = dt.datetime.combine(
            dt.date.fromisoformat(args.date), dt.time(hour=0), tzinfo=dt.timezone.utc
        )
    config = load_json(args.config)
    payload = build_radar(GitHubClient(token), config, args.output_root, now)
    log(
        f"Radar complete: {payload['stats']['candidate_count']} candidates, "
        f"mode={payload['ranking_mode']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
