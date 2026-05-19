# xiaohongshu_scraper_skill

> 小红书（RedNote）爬虫 Skill。三档签名 + 三档登录 + 浏览器接管的纵深防御架构。
> 支持笔记抓取、评论采集、图片/视频智能分析、Markdown/CSV 导出。

## 功能概览

| 能力 | 说明 |
|------|------|
| 笔记抓取 | 单篇详情 / 用户主页 / 关键词搜索 / 推荐流 |
| 评论采集 | 主评论 + 子评论分页，自动入库 |
| 媒体下载 | 图片自动下载 + 视频流式下载，按 `博主名/笔记标题/` 组织 |
| 图片分析 | OCR 文字提取 + AI 视觉描述 + Mermaid 路线图/流程图 |
| 视频分析 | 语音转文字 + 关键帧 OCR + AI 摘要 |
| 批量抓取 | 断点续抓 + `--download` + `--analyze` 自动化 |
| 多账号 | 账号轮换 + 日抓上限 + cookie 自动刷新 |
| 导出 | Markdown（含图片/视频/评论/分析）+ CSV（26 列，飞书可直接导入） |

## 快速开始

### 1. 安装（全自动）

```bash
cd xiaohongshu_scraper_skill

# 首次运行任何命令时自动安装所有依赖（Python 包、Node.js、crypto-js、Playwright）
python scripts/xhs.py setup
```

安装完成后运行诊断：

```bash
python scripts/xhs.py setup
# 显示所有核心依赖和可选依赖的安装状态
```

### 2. 配置分析能力（推荐）

```bash
# 统一引导向导：一次性配置图片+视频分析
python scripts/xhs.py setup-wizard
```

向导会引导你选择：

- **图片分析**：auto（自动判断）/ none（仅 OCR）/ local（本地分析）/ vision（AI 看图）
- **视频分析**：none / local / ollama / openai
- **AI 后端**：
  - Ollama（本地免费）→ 自动验证连接、模型、视觉能力
  - 云端 API（智谱/通义/硅基流动/DeepSeek/OpenAI）→ 自动验证 API Key

### 3. 登录

```bash
python scripts/xhs.py login --prefer rookie    # 从浏览器自动提取 cookie
python scripts/xhs.py login --prefer qr        # 扫码登录
python scripts/xhs.py login --prefer manual    # 手动粘贴 cookie
```

### 4. 抓取

```bash
# 单篇笔记
python scripts/xhs.py note 6603xxxxxxxxxxxxxxxxxxxx

# 关键词搜索
python scripts/xhs.py search "露营装备" --pages 3

# 用户笔记
python scripts/xhs.py user <user_id> --pages 5

# 推荐流
python scripts/xhs.py feed --category recommend --pages 2
```

### 5. 智能分析

```bash
# 图片分析（OCR + AI 视觉 + Mermaid 路线图）
python scripts/xhs.py analyze-images <note_id>

# 视频分析（语音转文字 + OCR + AI 摘要）
python scripts/xhs.py analyze-video <note_id>

# 批量：搜索 + 自动下载 + 自动分析
python scripts/xhs.py crawl-search "美食" --max-pages 5 --download --analyze
```

### 6. 导出

```bash
python scripts/xhs.py export --note <id> --format md    # 单篇 Markdown
python scripts/xhs.py export --format csv                # 全量 CSV（26 列）
```

## 项目结构

```
xiaohongshu_scraper_skill/
├── SKILL.md                   # 完整使用文档（给 AI Agent 用）
├── README.md                  # 本文件
├── requirements.txt
├── docs/
│   └── technical-report-image-analysis.md   # 图片分析技术报告
├── scripts/
│   ├── xhs.py                 # CLI 入口 + 命令调度
│   ├── xhs_config.py          # 统一配置 + 路径 + 共享工具
│   ├── xhs_fetcher.py         # Fetcher 核心类 + 错误处理
│   ├── xhs_api.py             # API 函数 + 数据标准化
│   ├── xhs_media.py           # 媒体下载 + 后处理
│   ├── xhs_storage.py         # SQLite + MD/CSV 渲染
│   ├── xhs_sign.py            # 三档签名
│   ├── xhs_login.py           # 三档登录 + 在线 cookie 验证
│   ├── xhs_accounts.py        # 多账号管理
│   ├── xhs_proxy.py           # 代理池
│   ├── xhs_log.py             # 请求日志与统计
│   ├── xhs_analyze.py         # 评论情感分析 & 话题聚类
│   ├── xhs_update_js.py       # JS 签名资产自动更新
│   ├── xhs_video.py           # 视频智能分析（语音转文字+OCR+AI摘要）
│   ├── xhs_image.py           # 图片智能分析（OCR+AI视觉+Mermaid图表）
│   └── xhs_bootstrap.py       # 首次运行自动安装依赖
├── assets/
│   ├── xhs_main.js            # 签名核心（来自 cv-cat/Spider_XHS）
│   ├── xhs_rap.js
│   └── xhs_xray.js
└── data/
    ├── xhs.db                 # SQLite
    ├── cookies.json           # 持久化 cookie
    ├── image_config.json      # 图片分析配置
    ├── video_config.json      # 视频分析配置
    ├── media/                 # 媒体文件：<博主名>/<笔记标题>/
    └── output/                # MD + CSV
```

## 图片分析

### 三层分析能力

```
Layer 1: OCR 文字提取    →  图片中所有可识别的文字（rapidocr，本地运行）
Layer 2: AI 视觉描述     →  AI "看懂"图片内容（路线、穿搭、步骤...）
Layer 3: Mermaid 图表    →  自动生成路线图/流程图（嵌入 Markdown）
```

### 自动判断模式（默认）

`image_mode: auto` 会根据每条笔记自动判断是否需要 AI：

| 情况 | 判定 |
|------|------|
| 旅游攻略（标题含"路线/攻略/行程"） | 需要 AI 看图 |
| 穿搭/教程（OCR 文字少但图片多） | 需要 AI 看图 |
| 纯文字图（OCR 提取到大量文字） | 跳过 AI，OCR 足够 |
| 零 OCR + 有图片 | 需要 AI 看图 |

### AI 视觉后端

| 后端 | 说明 | 配置 |
|------|------|------|
| Ollama | 本地免费，隐私优先 | 安装 Ollama + 下载视觉模型（如 qwen2-vl:7b） |
| API | 效果最好，按量付费 | 智谱 GLM-4V / 通义 Qwen-VL / 硅基流动 / DeepSeek / OpenAI |
| MCP 视觉工具 | AI Agent 提供，零配置 | 支持 Claude Code / GLM Coding / Cursor / Windsurf 等 |

配置时自动验证：Ollama 连接 → 模型存在 → 视觉能力检测。

## 视频分析

```
Layer 1: 语音转文字（faster-whisper，本地运行）
Layer 2: 关键帧 OCR（rapidocr，提取画面文字）
Layer 3: AI 摘要（none / local / ollama / openai / mcp 五档）
```

需要 ffmpeg（系统级工具，需手动安装）。

## 子命令速查

| 命令 | 用途 |
|------|------|
| `setup` | 安装依赖 + 诊断检查 |
| `setup-wizard` | 统一引导向导（图片+视频分析配置） |
| `setup-image` | 单独配置图片分析 |
| `setup-video` | 单独配置视频分析 |
| `login [--name <alias>]` | 登录/保存 cookie |
| `sign-test` | 签名健康检查 |
| `note <id>` | 抓单篇笔记 |
| `search <kw> --pages N` | 关键词搜索 |
| `user <id> --pages N` | 用户笔记列表 |
| `comments <id>` | 评论树 |
| `download <id>` | 下载图片/视频 |
| `analyze-images <id>` | 图片智能分析 |
| `analyze-video <id>` | 视频智能分析 |
| `feed --category <cat>` | 推荐流/分类流 |
| `crawl-search/user/feed` | 长任务断点续抓（支持 `--download --analyze`） |
| `export --format md/csv` | 导出 |
| `accounts` | 多账号状态 |
| `refresh-cookies` | 批量刷新 cookie |
| `update-js` | 签名 JS 热更新 |

## 设计要点

小红书反爬强度极高，签名算法约每月轮换。本项目采用**多策略可切换**架构：

- **签名层**：`PlaywrightSigner`（真实浏览器跑 JS）/ `EmbedJsSigner`（mini-racer 跑 xhs_main.js）/ `PyPortSigner`（纯 Python）三档自动降级
- **登录层**：rookiepy 浏览器 cookie → Playwright QR → 手动粘贴 三档 fallback
- **抓取层**：curl_cffi Chrome TLS 模拟 + xsec_token 全链路透传 + smart-pacing burst+rest 模型
- **风控应对**：连续 3 次 460 / 任一 461 自动切 Playwright 浏览器接管
- **速度**：normal / slow / paranoid 三档可调

## 签名 JS 热更新

小红书签名 JS 约**每月轮换**。轮换信号：所有签名档同时大量 460/406。

```bash
python scripts/xhs.py update-js          # 自动更新
python scripts/xhs.py update-js --dry-run # 只检查不覆盖
```

## 故障排查

| 现象 | 处理 |
|------|------|
| `sign-test` 全 FAIL | 检查 `assets/xhs_main.js` 是否过期，运行 `update-js` |
| `login` 报 `BrowserNotFound` | 用 `--prefer qr` 或 `--prefer manual` |
| 抓取返回 460 | 自动降速降级签名；全档失败等 24h 或换号 |
| 抓取返回 461 | 自动开浏览器过滑块 |
| Ollama 不可用 | 确认已安装 + 正在运行：`ollama serve` |
| API 调用失败 | 运行 `setup-wizard` 重新配置，自动验证连接 |
| ffmpeg 未安装 | 视频分析不可用，其他功能正常。Windows: `winget install Gyan.FFmpeg` |

## 风险

- **账号封禁**：强烈建议小号；单账号日抓硬上限 500
- **签名月度轮换**：内置三档兜底，极端情况下需等社区更新 JS
- **法律风险**：仅用于个人学习/研究，**不得**用于商业用途

## 参考项目

- [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) — JS 签名核心来源
- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) — Playwright 搭桥范式
- [jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli) — Python 签名参考

## 许可证

MIT
