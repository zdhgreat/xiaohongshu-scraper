# xiaohongshu_scraper_skill

> 小红书（RedNote）爬虫 Skill v3.0。五层纵深防御 + 智能内容分析 + Postgres 双写 + Hub 集成 + Agent 转录纠错。
> 支持笔记抓取、评论采集、图片/视频智能分析、Markdown/CSV 导出、Financial Hub 对接。

## 功能概览

| 能力 | 说明 |
|------|------|
| 笔记抓取 | 单篇详情 / 用户主页 / 关键词搜索 / 推荐流 |
| 评论采集 | 主评论 + 子评论分页，自动入库 |
| 媒体下载 | 图片自动下载 + 视频流式下载，按 `博主名/笔记标题/` 组织 |
| 图片分析 | OCR 文字提取 + AI 视觉描述 + Mermaid 路线图/流程图 |
| 视频分析 | 语音转文字 + 关键帧 OCR + 转录纠错（`correct` 命令） |
| 批量抓取 | 断点续抓 + `--download` + `--analyze` 自动化 |
| 多账号 | 账号轮换 + 日抓上限 + cookie 自动保活 |
| 导出 | Markdown（按博主分目录）+ CSV（26 列，飞书可直接导入） |
| 五层防御 | 签名稳定 + 传输稳定 + 会话稳定 + 账号稳定 + 可观测性 |
| DOM 搜索降级 | 搜索默认走真实浏览器 DOM 模式（搜索 API 仅降级使用） |
| Postgres 双写 | SQLite + Postgres 并行写入，数据实时同步到 Hub |
| Hub 集成 | 对接 financial_hub_postgres / financial_hub，爬虫生命周期管理 |
| 跨平台浏览器 | Windows（Edge/Chrome）+ macOS（Chrome/Edge）+ WSL（CDP 桥接） |

## 快速开始

### 1. 安装（全自动）

```bash
cd xiaohongshu_scraper_skill

# 首次运行任何命令时自动安装所有依赖
python scripts/xhs.py setup
```

自动安装：Python 依赖、Node.js、crypto-js、Playwright Chromium、jieba、rapidocr、faster-whisper。

**需手动安装**：ffmpeg（视频分析用，不装也能跑其他功能）

```bash
# Windows: winget install Gyan.FFmpeg
# macOS:   brew install ffmpeg
# Linux:   sudo apt install ffmpeg
```

### 2. 配置分析能力

```bash
python scripts/xhs.py setup-wizard   # 统一引导向导
```

### 3. 登录

```bash
# 推荐方式：QR 扫码（最简单，不需要提前在浏览器登录）
python scripts/xhs.py login --prefer qr --name account1   # 手机扫码登录
python scripts/xhs.py login --prefer qr --name account2   # 另一个号扫码

# 或者：从已登录的浏览器直接提取 cookie（秒级完成）
python scripts/xhs.py login --prefer rookie --name account1

# 或者：手动粘贴 cookie
python scripts/xhs.py login --prefer manual --name account1

# 验证账号
python scripts/xhs.py accounts
```

> **多账号说明**：每个号运行一次 `login --name <别名>`。QR 扫码最简单（手机扫一下就行）；浏览器提取需要先在浏览器里登录小红书。Cookie 有效期约 30 天，过期后重新运行 login 命令即可。

### 4. 抓取

```bash
python scripts/xhs.py search "露营装备" --pages 3 --download --analyze
python scripts/xhs.py user <user_id> --pages 5 --download --analyze
python scripts/xhs.py crawl-search "美食" --max-pages 50 --resume   # 长任务断点续抓
```

### 5. 导出

```bash
python scripts/xhs.py export --note <id> --format md    # 单篇 Markdown（多文件目录）
python scripts/xhs.py export --format csv                # 全量 CSV
```

## 五层防御架构

针对小红书"阿瑞斯"风控体系设计的纵深防御，长时间自主运行实测稳定。

```
Layer 1: 签名稳定
  ├─ embed-js 主签名（xhs_main.js，快速稳定）
  ├─ PlaywrightSigner 降级后备（真实浏览器签名）
  ├─ b1 令牌收割：Playwright 从浏览器 localStorage 提取 b1
  ├─ b1 注入：EmbedJsSigner 加载 b1_cache.json 替换 JS 中的 fff
  └─ 浏览器 2h 定时刷新：防止内存泄漏和会话老化

Layer 2: 传输稳定
  ├─ curl_cffi Chrome TLS 指纹模拟（JA3/JA4）
  ├─ -104 → b1 缓存刷新重试 → DOM 搜索降级
  ├─ 403 → 短等待重试（30-60s）
  ├─ 429 → 指数退避
  └─ 461 → 账号冷却 + 切换

Layer 3: 会话稳定
  ├─ 每 45-60 分钟休息 10-20 分钟（模拟人类离开）
  ├─ 每 10 次请求穿插辅助请求（homefeed/user/me）
  └─ 休息后重新 warmup

Layer 4: 账号稳定
  ├─ 主动轮换：账号活跃 40-60 分钟或 40 次请求后切换
  ├─ 窗口请求计数：防止轮换死循环
  └─ 每账号独立指纹（UA/sec-ch-ua/impersonate）

Layer 5: 可观测性
  ├─ 结构化 JSONL 请求日志
  ├─ 风控事件统计（460/461 计数）
  └─ 账号状态仪表盘（accounts 命令）
```

### b1 令牌管理（解决 -104 根因）

XHS 的 xhs_main.js 中硬编码了一个 `fff` 变量（b1 令牌的快照），服务端更新 b1 后旧 fff 失效 → -104。

解决方案：
1. **keepalive 时收割**：保活进程用 Playwright 从浏览器 localStorage 提取最新 b1 → 保存到 `data/b1_cache.json`
2. **EmbedJsSigner 注入**：启动时和定期从 `b1_cache.json` 加载 b1，替换 JS 中的 fff
3. **-104 紧急刷新**：搜索 API 返回 -104 时，先从缓存刷新 b1 重试，失败则降级到 DOM 搜索

### DOM 搜索（默认模式）

搜索默认走真实浏览器 DOM 模式，从 XHS 角度看就是正常用户浏览，风控风险极低。仅在需要时才走搜索 API（有独立小时配额限制）。

1. **真实浏览器模式**（优先）：用用户的 Edge/Chrome profile 启动 → 完整 cookies + localStorage → 页面 JS 自己调 API
2. **Playwright 降级模式**：注入 cookies 的 best-effort 方案

搜索 API 返回 -104 时也会自动切换到 DOM 模式。跨平台支持：Windows（Edge/Chrome）+ macOS（Chrome/Edge）。

## 实测稳定性

6 小时长跑测试（2 账号，search speed-mode，无代理）：

| 指标 | 结果 |
|------|------|
| 总入库笔记 | 640 条（16 页） |
| 总下载笔记 | 673 条 |
| 搜索 -104 → DOM 降级 | 17/17（100% 成功） |
| FatalRiskError | **0** |
| 账号主动轮换 | 3 次（49min / 53min / 63min） |
| 图片分析 | 正常（OCR + AI 视觉） |

## Postgres 双写 & Hub 集成

爬虫数据默认写入 SQLite（零配置），同时支持自动写入 Postgres 以对接 Financial Hub：

- **hub_adapter.py** — Postgres 连接 + SQLite→PG 同步 + Hub 爬虫生命周期管理
- **schema.sql** — Postgres 建表 DDL（xhs_notes / xhs_users / xhs_comments / xhs_search_cache / xhs_crawl_state；v3 新增 `status` / `updated_at` / `media_path` / `has_local_media` 字段，`init_schema` 自动幂等迁移）
- **.env.example** — Postgres 环境变量模板

Postgres 不可用时自动降级为仅 SQLite，不影响爬虫运行。

### 环境配置

```bash
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USER=xhs
export POSTGRES_PASSWORD=secret
export POSTGRES_DB=financial_hub   # 实际库名，如 financial_hub_v2
```

### 跨平台浏览器适配

登录和 DOM 搜索覆盖三大平台：

| 平台 | Edge | Chrome | WSL CDP |
|------|------|--------|---------|
| Windows | 原生提取 + DOM 搜索 | 原生提取 + DOM 搜索 | — |
| macOS | 原生提取 + DOM 搜索 | 原生提取 + DOM 搜索 | — |
| WSL | — | — | CDP 桥接到 Windows Edge/Chrome |

## 子命令速查

| 命令 | 用途 |
|------|------|
| `setup` | 安装依赖 + 诊断检查 |
| `setup-wizard` | 统一引导向导（图片+视频分析配置） |
| `health` | 系统健康检查（依赖+签名+账号+DB） |
| `login [--name <alias>]` | 登录/保存 cookie |
| `sign-test` | 签名健康检查 |
| `note <id>` | 抓单篇笔记 |
| `search <kw> --pages N` | 关键词搜索 |
| `user <id> --pages N` | 用户笔记列表 |
| `comments <id>` | 评论树（含子评论分页） |
| `download <id>` | 下载图片/视频 |
| `feed --category <cat>` | 推荐流/分类流 |
| `crawl-search/user/feed` | 长任务断点续抓（默认下载+分析） |
| `export --format md/csv/json/xlsx` | 导出 |
| `accounts` | 多账号状态 |
| `stats` | 请求统计 |
| `refresh-cookies` | 批量刷新 cookie |
| `keepalive [--daemon]` | Cookie 自动保活（单次或守护进程） |
| `update-js` | 签名 JS 热更新 |
| `analyze --type sentiment/topics` | 评论情感分析 / 话题聚类 |
| `setup-video` | 视频分析环境安装 |
| `analyze-video <id>` | 视频内容分析（转录+OCR+总结） |
| `correct --list/--note/--apply` | Agent 手动纠错视频转录（列出待纠错 / 读取单条 / 写回纠错结果） |
| `enrich` | 补全搜索入库的半成品笔记 |
| `refresh` | 重抓超过 N 小时的旧笔记 |
| `cleanup` | 数据清理（孤儿媒体+过期缓存+VACUUM） |
| `update-fp` | 全量更新 UA/指纹池/TLS/签名JS |
| `serve` | 守护进程模式：自动循环爬取 |

通用参数：

```
--sign-mode {auto,embed-js,playwright}   # 默认 auto
--speed-mode {paranoid}                   # 当前仅 paranoid（最安全）
--account <alias>                        # 指定账号
```

## 图片分析

```
Layer 1: OCR 文字提取    →  rapidocr，本地运行
Layer 2: AI 视觉描述     →  Ollama / OpenAI 兼容 API / MCP 视觉工具
Layer 3: Mermaid 图表    →  自动生成路线图/流程图
```

`image_mode: auto` 自动判断是否需要 AI（旅游攻略/穿搭 → 需要，纯文字图 → 跳过）。

## 视频分析

```
Layer 1: 语音转文字（faster-whisper）
Layer 2: 关键帧 OCR（rapidocr）
Layer 3: 转录纠错（`correct` 命令：Agent 读转录+OCR 纠同音字；可选 `--correct-mode` 配 LLM 后端）
```

> 视频笔记入库后，数据写入命令退出前自动打印 `⚠️ [CORRECT] 待纠错 N 条`，提示用 `correct --list` / `correct --note <id> --apply` 纠错。Whisper base 中文同音字错误多，**未纠错的转录不可直接使用**。

## 项目结构

```
xiaohongshu_scraper_skill/
├── SKILL.md                 # AI Agent 完整使用文档
├── README.md                # 本文件
├── requirements.txt
├── scripts/
│   ├── xhs.py               # CLI 入口 + 命令调度
│   ├── xhs_config.py        # 统一配置 + 路径 + 指纹池
│   ├── xhs_fetcher.py       # Fetcher（TLS+节流+风控+DOM搜索+会话管理+账号轮换）
│   ├── xhs_sign.py          # 三档签名 + b1 收割/注入 + auto 路由
│   ├── xhs_api.py           # API 调用层 + 数据标准化
│   ├── xhs_media.py         # 媒体下载 + 后处理编排
│   ├── xhs_storage.py       # SQLite 存储 + Markdown/CSV 渲染
│   ├── xhs_accounts.py      # 多账号池（LRU 轮换 + 冷却 + 指纹绑定）
│   ├── xhs_proxy.py         # 代理池（轮换 + 指数冷却）
│   ├── xhs_login.py         # 跨平台多档登录 fallback
│   ├── xhs_login_native.py  # 跨平台浏览器 Cookie 提取
│   ├── xhs_login_wsl.py     # WSL 环境 CDP 桥接登录
│   ├── xhs_keepalive.py     # Cookie 自动保活 + b1 收割
│   ├── xhs_parallel.py      # 多账号并行爬取
│   ├── xhs_log.py           # 结构化 JSONL 请求日志
│   ├── xhs_analyze.py       # 评论情感分析 + 话题聚类
│   ├── xhs_update_js.py     # 签名 JS 自动更新
│   ├── xhs_video.py         # 视频分析（语音转写 + OCR + 转录纠错）
│   ├── xhs_image.py         # 图片分析（OCR + AI 视觉 + Mermaid）
│   ├── xhs_bootstrap.py     # 依赖自动安装
│   ├── query_db.py          # PostgreSQL 只读查询工具
│   ├── xhs_risk_status.py   # 风控状态监控
│   └── xhs_parallel.py      # 多账号并行爬取
├── hub_adapter.py            # PG 连接 + SQLite→PG 同步 + Hub 集成
├── schema.sql                # Postgres 建表 DDL
├── .env.example              # Postgres 环境变量模板
├── assets/                   # 签名 JS 资产
│   ├── xhs_main.js           # 主签名算法（社区维护）
│   ├── xhs_rap.js            # x-rap-param 签名
│   ├── xhs_xray.js           # x-xray-traceid 签名
│   ├── xhs_xray_pack1.js     # xhs_xray.js webpack bundle 1
│   ├── xhs_xray_pack2.js     # xhs_xray.js webpack bundle 2
│   ├── xhs_a1.js             # a1 签名生成
│   ├── crypto-js.min.js      # CryptoJS
│   └── package.json          # Node 依赖（crypto-js）
└── data/                     # 运行时数据（自动生成）
    ├── accounts/<alias>.json # 多账号 Cookie
    ├── accounts_state.json   # 账号运行时状态
    ├── b1_cache.json         # b1 令牌缓存
    ├── xhs.db                # SQLite 数据库
    ├── runs.jsonl             # 请求日志
    ├── media/                # 媒体：<博主名>/<笔记标题>/
    └── output/               # 导出：MD/CSV/XLSX
```

## 故障排查

| 现象 | 处理 |
|------|------|
| `sign-test` 全 FAIL | 运行 `update-js` 更新签名 JS |
| 搜索返回 -104 | 自动降级到 DOM 搜索（真实浏览器），无需干预 |
| 非搜索 API 返回 -104 | 自动刷新 b1 缓存重试一次 |
| 抓取返回 460 | 自动降速 + 切账号 + 切代理 |
| 抓取返回 461 | 账号冷却 4 小时 + 切账号（461 = 被判定为自动化行为） |
| `database is locked` | 前一个 xhs.py 进程未退出；用 `taskkill`/`pkill` 结束残留进程后重试（WAL 模式会自动恢复） |
| Ollama 不可用 | 确认 `ollama serve` 正在运行 |
| ffmpeg 未安装 | 视频分析降级为仅 OCR，其他功能正常 |

## 风险

- **账号封禁**：强烈建议小号；单账号 API 日抓硬上限 80，DOM 搜索 100 页/天
- **签名月度轮换**：约每月一次，内置三档兜底
- **法律风险**：仅用于个人学习/研究，不得用于商业用途

## 参考项目

- [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) — JS 签名核心来源
- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) — Playwright 搭桥范式

## 许可证

MIT
