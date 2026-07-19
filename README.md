# GitHub Weekly Radar

每周自动发现值得关注的开源项目，并生成两张跨赛道 Top 10 榜单：

1. **近 30 天新项目 Star Top 10**：在最近 30 天创建的项目中，按当前 Star 总数排序。
2. **7 天增长 Top 10**：以本周与上周快照的 Star 差值为主、增长率为辅排序。

关注三个赛道：

- AI 与智能体
- 医疗数智化
- 效率与开发工具

## 工作方式

GitHub Actions 每周一北京时间 08:00 自动运行：

1. 按 Topic 搜索三个赛道的候选项目，并补充 GitHub Trending 周榜候选。
2. 排除 Fork、归档、无许可证、长期不活跃、纯资源清单、课程、镜像、纯数据集等仓库。
3. 保存本周 Star 快照，与上周快照计算增量。
4. 生成 `data/latest.json`、`reports/latest.md` 和带日期的历史文件。
5. 自动提交本周数据和报告。

第一次运行没有历史快照，因此增长榜会以“创建以来日均 Star”生成观察名单；从第二次运行开始切换为真实周增量。

## 目录

```text
.
├── .github/workflows/weekly-radar.yml  # 每周自动任务
├── config/radar.json                   # 赛道、关键词和筛选参数
├── data/latest.json                    # 最新结构化榜单
├── data/snapshots/                     # 每周候选池快照
├── reports/latest.md                   # 最新可读榜单
├── reports/YYYY-MM-DD.md               # 历史榜单
├── scripts/radar.py                    # 采集、筛选、排名和报告生成
└── tests/test_radar.py                  # 核心规则测试
```

## 手动运行

GitHub Actions 中选择 **Weekly Open Source Radar**，点击 **Run workflow** 即可。

本地运行需要 Python 3.11+ 和可读取公开仓库的 GitHub Token：

```bash
export GITHUB_TOKEN=your_token
python scripts/radar.py
python -m unittest discover -s tests -v
```

GitHub Actions 会自动提供 `GITHUB_TOKEN`，无需额外创建密钥。

## 榜单字段

每个项目记录：名称、链接、赛道、简介、主要语言、许可证、总 Star、7 天新增、增长率、创建/更新时间、入榜来源，以及是否双榜上榜。

ChatGPT 决策版解读提示词见 [`docs/chatgpt-report-prompt.md`](docs/chatgpt-report-prompt.md)。

## License

MIT
