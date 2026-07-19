import datetime as dt
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("radar", ROOT / "scripts" / "radar.py")
radar = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(radar)


def repository(
    full_name="example/project",
    *,
    stars=100,
    created_at="2026-07-10T00:00:00Z",
    pushed_at="2026-07-18T00:00:00Z",
    topics=None,
    description="An AI agent for clinical workflow",
    license="MIT",
):
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "owner": owner,
        "html_url": f"https://github.com/{full_name}",
        "description": description,
        "language": "Python",
        "topics": topics or ["ai-agent"],
        "license": license,
        "stars": stars,
        "forks": 5,
        "open_issues": 3,
        "created_at": created_at,
        "updated_at": pushed_at,
        "pushed_at": pushed_at,
        "fork": False,
        "archived": False,
        "disabled": False,
        "matched_categories": ["ai_agent"],
        "sources": ["test"],
        "category_key": "ai_agent",
        "category": "AI与智能体",
    }


def api_repository(full_name="example/project", *, stars=100):
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "html_url": f"https://github.com/{full_name}",
        "description": "An AI agent framework",
        "language": "Python",
        "topics": ["llm", "ai-agent"],
        "license": {"spdx_id": "MIT"},
        "stargazers_count": stars,
        "forks_count": 5,
        "open_issues_count": 2,
        "created_at": "2026-07-10T00:00:00Z",
        "updated_at": "2026-07-25T00:00:00Z",
        "pushed_at": "2026-07-25T00:00:00Z",
        "fork": False,
        "archived": False,
        "disabled": False,
    }


class FakeClient:
    def __init__(self, stars):
        self.stars = stars

    def search_repositories(self, query, *, sort="stars", per_page=60):
        return [api_repository(stars=self.stars)]

    def weekly_trending_names(self):
        return []

    def get_repository(self, full_name):
        return api_repository(full_name, stars=self.stars)

    def has_readme(self, full_name):
        return True


class RadarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config" / "radar.json").read_text(encoding="utf-8"))
        cls.now = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)

    def test_eligibility_accepts_active_licensed_project(self):
        self.assertTrue(radar.is_eligible(repository(), self.config, self.now))

    def test_eligibility_rejects_resource_list_and_missing_license(self):
        awesome = repository(
            "example/awesome-ai",
            topics=["awesome-list"],
            description="A curated list of AI resources",
        )
        unlicensed = repository("example/no-license", license="")
        self.assertFalse(radar.is_eligible(awesome, self.config, self.now))
        self.assertFalse(radar.is_eligible(unlicensed, self.config, self.now))

    def test_eligibility_rejects_inactive_project(self):
        inactive = repository("example/old", pushed_at="2025-01-01T00:00:00Z")
        self.assertFalse(radar.is_eligible(inactive, self.config, self.now))

    def test_medical_category_wins_for_medical_ai_project(self):
        repo = repository(
            "example/clinical-agent",
            topics=["ai-agent", "healthcare", "medical-ai"],
            description="Clinical healthcare AI agent",
        )
        repo["matched_categories"] = ["ai_agent", "medical_digital"]
        key, label = radar.classify_repository(repo, self.config)
        self.assertEqual(key, "medical_digital")
        self.assertEqual(label, "医疗数智化")

    def test_unrelated_trending_repository_has_no_category(self):
        repo = repository(
            "example/unrelated-design-tool",
            topics=["design", "css"],
            description="A visual design system for websites",
        )
        repo["matched_categories"] = []
        self.assertIsNone(radar.classify_repository(repo, self.config))

    def test_cold_start_uses_daily_star_average(self):
        young = repository("example/young", stars=200, created_at="2026-07-17T00:00:00Z")
        older = repository("example/older", stars=500, created_at="2026-06-19T00:00:00Z")
        _, hot, mode = radar.rank_repositories([older, young], {}, self.config, self.now)
        self.assertEqual(mode, "cold_start_daily_average")
        self.assertEqual(hot[0]["full_name"], "example/young")

    def test_weekly_growth_sorts_delta_then_rate(self):
        first = repository("example/first", stars=200)
        second = repository("example/second", stars=300)
        third = repository("example/third", stars=150)
        previous = {
            "example/first": {"full_name": "example/first", "stars": 100},
            "example/second": {"full_name": "example/second", "stars": 200},
            "example/third": {"full_name": "example/third", "stars": 100},
        }
        _, hot, mode = radar.rank_repositories(
            [third, second, first], previous, self.config, self.now
        )
        self.assertEqual(mode, "weekly_delta")
        self.assertEqual([item["full_name"] for item in hot], ["example/first", "example/second", "example/third"])

    def test_public_entry_marks_double_ranking(self):
        repo = repository()
        repo.update(
            {
                "previous_stars": 80,
                "weekly_star_delta": 20,
                "weekly_growth_rate": 0.25,
                "daily_star_average": 10.0,
            }
        )
        entry = radar.prepare_public_entry(repo, 1, True)
        self.assertTrue(entry["double_ranked"])
        self.assertEqual(entry["weekly_star_delta"], 20)

    def test_two_week_build_switches_from_cold_start_to_weekly_delta(self):
        config = copy.deepcopy(self.config)
        config["categories"] = {"ai_agent": config["categories"]["ai_agent"]}
        config["categories"]["ai_agent"]["topics"] = ["llm"]
        config["search_delay_seconds"] = 0
        config["include_github_trending"] = False

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            first = radar.build_radar(
                FakeClient(100),
                config,
                output_root,
                dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc),
            )
            second = radar.build_radar(
                FakeClient(145),
                config,
                output_root,
                dt.datetime(2026, 7, 26, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(first["ranking_mode"], "cold_start_daily_average")
            self.assertEqual(second["ranking_mode"], "weekly_delta")
            self.assertEqual(
                second["lists"]["weekly_growth"][0]["weekly_star_delta"], 45
            )
            self.assertTrue((output_root / "reports" / "latest.md").exists())
            self.assertTrue((output_root / "data" / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
