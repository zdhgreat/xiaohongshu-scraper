# xiaohongshu-scraper

> 小红书（RedNote）爬虫 Skill v3.0。适配 Claude Code / Codex / Hermes 等 Agent 环境。
> 抓取笔记 / 用户 / 搜索 / 评论，视频转录 + OCR + Agent 纠错，自动同步 PostgreSQL，导出 Markdown / CSV。

## 30 秒开始

```bash
npx skills add https://github.com/zdhgreat/xiaohongshu-scraper
```

或把这段话发给有 shell 权限的 AI Agent：

> 帮我安装 xiaohongshu-scraper。克隆 https://github.com/zdhgreat/xiaohongshu-scraper 到 skills 目录，跑 `python scripts/xhs.py setup` 装依赖。

装好后直接对 Agent 说：

> 帮我抓小红书用户 `<user_id>` 最新 5 条笔记，下载视频并转录。

## 能做什么

- 📥 **抓取**：单篇详情 / 用户主页 / 关键词搜索 / 推荐流，支持断点续抓
- 💬 **评论**：主评论 + 子评论分页，自动入库
- 🎬 **媒体**：图片 + 视频流式下载，按 `博主/笔记标题/` 组织
- 🎤 **视频分析**：faster-whisper 转录 + 关键帧 OCR
- ✏️ **Agent 转录纠错**：`correct` 命令读转录 + OCR → Agent 纠同音字 → 写回；数据写入后自动提示待纠错数
- 🔄 **Postgres 双写**：SQLite + 自动同步 financial_hub（含 Hub 生命周期打卡 + 统计回灌）
- 📤 **导出**：Markdown（多文件目录）/ CSV（飞书格式）/ JSON / XLSX
- 🛡️ **反风控**：多账号轮换 + 签名三档兜底 + DOM 搜索降级
- 🖥️ **跨平台**：Windows / macOS / WSL 浏览器 Cookie 提取

## 适合 / 不适合

✅ **适合**：博主笔记归档、视频转录留档、评论情感分析、内容选题调研、选题监控
❌ **不适合**：大规模商业采集（单账号日上限 80 API / 100 DOM）、实时监控、纯公开 API 场景

## 使用流程

1. **登录**：`xhs.py login --prefer qr --name acc1`（扫码）或 `--prefer rookie`（浏览器提取 cookie）
2. **抓取 + 分析**：`xhs.py user <id> --pages 3 --download --analyze`
3. **纠错**：看到 `⚠️ [CORRECT] 待纠错 N 条` → `xhs.py correct --list` → `correct --note <id> --apply "..."`
4. **导出**：`xhs.py export --note <id> --format md`
5. **同步 PG**：自动（数据写入即同步），或手动 `python hub_adapter.py`

## 命令速查

| 命令 | 用途 |
|---|---|
| `login [--prefer qr/rookie/manual]` | 登录保存 cookie |
| `user <id> / search <kw> / note <id>` | 抓笔记（可加 `--download --analyze`） |
| `comments <id>` | 评论树 |
| `download <id>` | 下载媒体 |
| `analyze-video <id>` | 视频转录 + OCR |
| `correct --list / --note / --apply` | Agent 转录纠错（v3.0 新） |
| `crawl-search / crawl-user / crawl-feed` | 长任务断点续抓 |
| `export --format md/csv/json/xlsx` | 导出 |
| `accounts / health / sign-test` | 账号 / 健康 / 签名检查 |
| `keepalive / update-js / cleanup` | Cookie 保活 / 签名更新 / 数据清理 |
| `serve` | 守护进程循环爬取 |

## 配置

复制 `.env.example` 为 `.env` 填 Postgres 连接（不配则只用本地 SQLite）：

```
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=xhs
POSTGRES_PASSWORD=secret
POSTGRES_DB=financial_hub   # 实际库名，如 financial_hub_v2
```

PG 不可用时自动降级为仅 SQLite，不影响爬取。视频分析需 [ffmpeg](https://ffmpeg.org/)（不装则跳过转录，仅 OCR）。

## 目录结构

```
xiaohongshu-scraper/
├── SKILL.md              # Agent 完整使用文档
├── scripts/xhs.py        # CLI 入口 + 命令调度
├── hub_adapter.py        # PG 同步 + Hub 集成
├── schema.sql            # PG 建表 DDL
├── assets/               # 签名 JS
└── data/                 # 运行时（xhs.db / media / output，自动生成）
```

## FAQ

**转录有同音字错误怎么办？** Whisper base 中文局限。`xhs.py correct --list` 读转录 + OCR，Agent 纠正后 `correct --note <id> --apply "..."` 写回。

**PG 里没数据？** 检查 `.env` 的 `POSTGRES_*`；数据写入命令会自动同步，也可手动 `python hub_adapter.py`。

**签名失效？** `xhs.py update-js` 更新签名 JS。

**抓取被风控？** 默认 paranoid 模式（每请求 4-10 分钟）+ 多账号轮换；`xhs.py accounts` 查健康度。

## License

MIT
