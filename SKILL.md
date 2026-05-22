---
name: xiaohongshu-scraper
description: 抓取小红书（RedNote/Little Red Book/XHS）笔记、用户主页、关键词搜索结果，落库 SQLite 并导出 Markdown / CSV（飞书多维表格格式）。在用户提到"爬小红书"、"采集小红书笔记"、"抓 xhs"、"分析小红书数据"、"导出小红书到飞书"、"备份我喜欢的小红书博主"、"收集某关键词下的小红书内容"等场景时调用。三档签名（playwright/embed-js/py-port）+ 跨平台登录（rookiepy/原生浏览器提取/QR/手动）+ 浏览器接管 的纵深防御架构，应对小红书强反爬。
version: "1.6.0"
author: zhudonghai
type: skill
tags:
  - xiaohongshu
  - rednote
  - xhs
  - scraper
  - crawler
  - content-collection
  - feishu
  - data-export
license: MIT
entrypoint: scripts/xhs.py
---

# Xiaohongshu Scraper Skill

抓取小红书（小红书 / RedNote / 小红薯 / XHS）的笔记、用户、搜索结果，输出可用于内容分析、飞书表格、个人备份的结构化数据。

---

## 何时调用此 Skill（When to Use）

当用户表达以下任一意图时调用：

| 用户原话模式 | 示例 |
|---|---|
| "爬/抓/采集小红书…" | "帮我爬一下博主 XXX 的笔记" |
| "想要 / 收集 / 备份小红书内容" | "我想把我喜欢的博主的所有笔记备份下来" |
| "导出小红书到飞书 / Excel / CSV" | "把这批笔记导成 CSV 我要导飞书" |
| "分析小红书某话题 / 关键词" | "搜索'露营'关键词下前 50 条笔记给我看" |
| "查某用户 / 看某笔记 / 拿某 ID" | "把这个 https://www.xiaohongshu.com/explore/xxx 的内容抓下来" |
| 提到 `note_id`、`user_id`、`xsec_token`、`a1`、`web_session` 等小红书技术词汇 | — |

**不要**用此 Skill 的场景：
- 抖音 / B 站 / 微博 / 知乎 等其他平台（除非用户明确切换到小红书）
- 自动发布 / 评论 / 点赞 / 关注 等账号操作（本 Skill 仅采集，不写）
- 任何用于商业用途的大规模采集（仅供个人学习研究）

---

## 工作流（Workflow）

### Step 0：环境自动准备（首次运行自动触发，无需手动操作）

**首次运行任何命令时，爬虫会自动检测并安装所有依赖**（Python 包、Node.js、crypto-js、Playwright Chromium）。整个过程约 2-5 分钟，取决于网络速度。

也可以手动触发完整安装和检查：

```bash
python scripts/xhs.py setup
```

**自动安装内容**：

| 组件 | 用途 | 安装方式 |
|---|---|---|
| Python 依赖 | requests, curl_cffi, PyExecJS, cryptography 等 | `pip install -r requirements.txt` |
| Node.js | 签名引擎 PyExecJS 需要 | winget / brew / apt（按平台自动选择） |
| crypto-js | 签名算法核心依赖 | `npm install`（在 assets/ 目录） |
| Playwright + Chromium | QR 登录 / 浏览器接管 / PlaywrightSigner | `playwright install chromium` |
| jieba | 图片/视频 本地文本分析（关键词提取） | `pip install -r requirements.txt`（含） |
| rapidocr-onnxruntime | 图片 OCR 文字识别 + 视频帧 OCR | `pip install -r requirements.txt`（含） |
| faster-whisper | 视频语音转文字 | `pip install -r requirements.txt`（含） |

安装完成后会自动验证所有包是否可导入，缺失的会列出提示。

**需要手动安装的组件**：

| 组件 | 用途 | 说明 |
|---|---|---|
| ffmpeg | 视频分析（语音提取+关键帧抽帧） | 系统级工具，需手动安装 |

```bash
# ffmpeg 安装
#   Windows: winget install Gyan.FFmpeg
#   macOS:   brew install ffmpeg
#   Linux:   sudo apt install ffmpeg
```

> 未安装 ffmpeg 时，视频分析会降级为仅 OCR，但 analyze-video 步骤仍需执行。

**配置图片/视频分析**（安装完依赖后运行）：

```bash
# 推荐：统一引导向导，一次性配置图片+视频
python scripts/xhs.py setup-wizard

# 或单独配置
python scripts/xhs.py setup-image   # 图片分析
python scripts/xhs.py setup-video   # 视频分析
```

**手动安装（仅在自动安装失败时需要）**：

```bash
# Python 依赖
pip install -r requirements.txt

# Node.js（按平台选择）
#   Windows: winget install OpenJS.NodeJS
#   macOS:   brew install node
#   Linux:   sudo apt install nodejs

# crypto-js
cd assets && npm install && cd ..

# Playwright（QR 登录需要，不做 QR 可跳过）
playwright install chromium
# Linux/WSL 系统依赖:
sudo playwright install-deps chromium
```

**注意**：`rookiepy` 在 Python 3.13+ 暂无 wheel（需要 Rust 工具链编译），requirements.txt 已加 `python_version < "3.13"` 限定。若你是 Python 3.13+，登录请用 `--prefer manual` 或 `--prefer qr`。

### Step 1：签名健康检查（每次会话开始时强制）

```bash
# Windows 用 python，macOS/Linux 用 python3
python scripts/xhs.py sign-test
```

判断输出：
- 至少 `[embed-js] OK` 或 `[playwright] OK` 其一 → 继续
- 两者都 FAIL → **JS 已过期**，转到「签名 JS 月度更新」节，不要继续抓
- 抓取过程中持续出现 460 → 同上

### Step 2：登录（cookie 不存在或失效时）

```bash
python scripts/xhs.py login                    # auto 模式：自动选择最优（rookiepy → 原生提取 → QR → 手动）
python scripts/xhs.py login --prefer rookie    # 从已登录的本地浏览器自动提取（推荐，跨平台）
python scripts/xhs.py login --prefer edge      # 从 Edge 浏览器提取 cookie（跨平台）
python scripts/xhs.py login --prefer chrome    # 从 Chrome 浏览器提取 cookie（跨平台）
python scripts/xhs.py login --prefer qr        # 弹 Chromium 让用户扫码（需 GUI）
python scripts/xhs.py login --prefer manual    # 让用户从 DevTools 粘贴 cookie 字符串
python scripts/xhs.py login --name <alias>     # 多账号：保存到 data/accounts/<alias>.json
```

`--prefer` 完整选项：`auto`（自动选择最优）、`rookie`（rookiepy 跨平台提取）、`edge`/`chrome`（原生浏览器提取，跨平台）、`native`（Edge + Chrome 依次尝试）、`qr`（扫码）、`manual`（手动粘贴）、`wsl-*`（WSL 环境专用）。

`data/cookies.json` 存在且包含 `web_session` 和 `a1` 即视为登录有效。Cookie 有效期约 30 天。

### 多账号授权

> 多号爬取能显著提升日抓上限（每个号 500/天，3 个号 = 1500/天），且 460/461 风控时自动切换。

**推荐方式一：浏览器提取（跨平台，最稳定）**

先用真实浏览器（Chrome / Edge / Firefox）登录 xiaohongshu.com，然后提取 cookie：

```bash
# 自动模式：rookiepy 直接读取浏览器 cookie（无需关闭浏览器）
python scripts/xhs.py login --name account1
# → 换浏览器账号或用无痕窗口登录另一个号
python scripts/xhs.py login --name account2

# 指定浏览器
python scripts/xhs.py login --prefer edge --name edge_account     # 从 Edge 提取
python scripts/xhs.py login --prefer chrome --name chrome_account # 从 Chrome 提取
```

**推荐方式二：扫码登录**

```bash
# 每个号扫一次码即可，不需要切换浏览器
python scripts/xhs.py login --prefer qr --name account1
# → 弹出 Chromium → 用手机 A 扫码确认

python scripts/xhs.py login --prefer qr --name account2
# → 弹出新的 Chromium（独立 profile）→ 用手机 B 扫码确认

# 同一部手机也可以：在小红书 App 里切换账号后扫第二个码
```

每个 `--name` 会用独立的浏览器 profile（`data/pw_profile_<name>`），不会互相干扰。

**注意**：Playwright 内置 Chromium 的扫码登录 session 可能不稳定（小红书反爬检测），建议优先使用浏览器提取方式。

**方式三：多浏览器提取（跨平台）**

如果 Edge 和 Chrome 分别登录了不同的小红书账号：

```bash
python scripts/xhs.py login --prefer edge --name edge_account
python scripts/xhs.py login --prefer chrome --name chrome_account
```

**方式三：手动粘贴（通用）**

适合任何环境，不需要额外依赖：

```bash
python scripts/xhs.py login --prefer manual --name account1
# → 提示粘贴 cookie → 在浏览器登录小红书 → F12 → Application → Cookies → 复制全部

python scripts/xhs.py login --prefer manual --name account2
# → 切换浏览器账号或用无痕窗口 → 重复上述步骤
```

**添加完账号后验证**：

```bash
python scripts/xhs.py accounts
# 输出:
#   [account1       ] 日抓   0/500  累计     0  460×0  461×0
#   [account2       ] 日抓   0/500  累计     0  460×0  461×0
```

**自动轮换行为**：运行任何爬取命令时，系统自动选择最久未用的可用账号。触发 460/461 风控时自动冷却当前号并切换到下一个。不需要手动指定账号。

### Step 3：抓取（按用户意图分流）

| 用户意图 | 命令 | 说明 |
|---|---|---|
| 抓单条笔记 | `python scripts/xhs.py note <note_id> [--xsec-token <token>]` | 部分笔记需 xsec_token（从分享链接 URL 参数取）；DB 里有时自动复用 |
| 抓某用户笔记列表 | `python scripts/xhs.py user <user_id> --pages 3 --download --analyze` | 前 N 页，每页 30 条；加 `--download --analyze` 自动下载+分析 |
| 抓关键词搜索 | `python scripts/xhs.py search "<关键词>" --pages 2 --download --analyze` | 前 N 页，每页 20 条；加 `--download --analyze` 自动下载+分析 |
| 抓某笔记评论 | `python scripts/xhs.py comments <note_id> --max-pages 5 --max-sub-pages 3 [--no-sub]` | 含子评论分页；`--no-sub` 跳过子评论分页 |
| 下载某笔记图片/视频 | `python scripts/xhs.py download <note_id> [--no-video] [--overwrite]` | `--no-video` 不下载视频；`--overwrite` 重新下载已有文件 |
| 推荐流/分类流 | `python scripts/xhs.py feed --category <cat> --pages 2 [--num 18]` | 分类见下方；`--num` 每页条数 |
| **长任务** 关键词 + 断点续抓 | `python scripts/xhs.py crawl-search "<kw>" --max-pages 20 [--resume]` | 风控/中断后 `--resume` 接续 |
| **长任务** 用户全部笔记 | `python scripts/xhs.py crawl-user <user_id> --max-pages 50 [--resume]` | 同上 |
| 视频内容智能分析 | `python scripts/xhs.py analyze-video <note_id>` | 语音转文字 + 关键帧 OCR + AI 摘要 |
| 配置视频分析 | `python scripts/xhs.py setup-video` | 交互选择 AI 摘要模式、Whisper 模型等 |

> **单篇笔记完整处理**：抓取单条笔记时，必须依次执行 note → download → comments → analyze-images/analyze-video → export。详见「单篇笔记完整处理流程（强制标准）」节。

**feed --category 可选值**：`recommend`（推荐，默认）、`food`（美食）、`fashion`（穿搭）、`travel`（旅行）、`beauty`（美妆）、`fitness`（健身）。

**通用参数**（默认值是最稳的，**首次或风控强时强烈建议加** `--speed-mode paranoid`）：

```
--sign-mode {auto, embed-js, playwright, py-port}   # 默认 auto，自动选最优并降级
--speed-mode {normal, slow, paranoid}               # 默认 normal（3-7s/请求）
--proxy http://host:port                            # 走代理
--account <alias>                                   # 多账号时指定账号别名
```

### Step 4：内容智能分析（每条笔记必须执行）

**所有笔记都必须经过分析步骤**，这是输出质量的核心保证，不是可选环节。

| 笔记类型 | 必须执行的分析命令 | 说明 |
|---|---|---|
| 图文笔记 | `analyze-images` | OCR 文字提取 + AI 视觉描述 |
| 视频笔记 | `analyze-video` | 语音转文字 + 关键帧 OCR + AI 摘要 |

```bash
# 图文笔记
python scripts/xhs.py analyze-images <note_id>

# 视频笔记
python scripts/xhs.py analyze-video <note_id>
```

> **批量模式（推荐）**：使用 `user` / `search` / `crawl-*` 命令时加 `--download --analyze`，在同一个进程内自动完成下载+分析，避免数据库锁冲突：
> ```bash
> python scripts/xhs.py user <user_id> --pages 3 --download --analyze
> python scripts/xhs.py search "关键词" --pages 2 --download --analyze
> python scripts/xhs.py crawl-user <user_id> --max-pages 20
> python scripts/xhs.py crawl-search "关键词" --max-pages 5
> ```
>
> **注意**：以上命令含视频分析时，单条视频需要 30-120 秒。执行时请设置充足 timeout（建议 600 秒以上），或使用 crawl 命令让 skill 内部调度。
>
> ⚠️ **串行执行原则**：所有命令共享同一个 SQLite 数据库，不支持同时运行。等上一个命令完全结束后再启动下一个。如果命令超时，skill 会自动检测并清理残留锁，无需手动干预。

> **分析依赖**（未安装时自动降级为仅 OCR）：ffmpeg（视频音频提取）、faster-whisper（语音转文字）、rapidocr-onnxruntime（OCR）。即便依赖不完整，analyze 步骤也必须执行——至少会产出 OCR 结果。

### Step 5：导出（按用户需要的格式）

```bash
python scripts/xhs.py export --note <note_id> --format md    # 单篇 Markdown（多文件目录）
python scripts/xhs.py export --format csv                    # 全量 CSV（按博主分文件）
python scripts/xhs.py export --format csv --user <user_id>   # 指定博主的 CSV
python scripts/xhs.py export --format json                   # 全量 JSON
python scripts/xhs.py export --format json --user <user_id>  # 指定博主的 JSON
python scripts/xhs.py export --format xlsx                   # 全量 XLSX（多 sheet，需 openpyxl）
```

输出位置：`data/output/`（Markdown 按博主分子目录，CSV 按博主分文件）

### Step 6：逐条报告（重要原则）

**禁止**："先把 100 条全部抓完，最后统一报告"。
**正确**：每抓完一条立刻在终端打印一行（命令自带），完整入库后再抓下一条。这样用户随时可中断且已完成的数据已落盘。

---

## 子命令速查表

| 命令 | 用途 | 需要登录 |
|---|---|---|
| `setup` | 安装所有依赖（首次运行自动触发，此命令用于排查） | 否 |
| `sign-test` | 三档签名健康检查 | 否 |
| `login [--prefer <mode>] [--name <alias>]` | 获取并落库 cookie（`--name` 保存到 data/accounts/） | 否 |
| `note <id>` | 单笔记详情入库 + MD 输出 | 是 |
| `user <id> --pages N [--download] [--analyze]` | 用户信息 + 笔记列表前 N 页入库 | 是 |
| `search <kw> --pages N [--download] [--analyze]` | 关键词搜索前 N 页入库 | 是 |
| `comments <id> [--max-pages N] [--max-sub-pages N] [--no-sub]` | 评论树（含子评论分页，`--no-sub` 跳过子评论） | 是 |
| `download <id> [--no-video] [--overwrite]` | 图片/视频本地化 | 否 |
| `feed --category <cat> --pages N [--num N]` | 推荐流/分类流浏览入库 | 是 |
| `crawl-search <kw> --max-pages N [--resume] [--no-analyze]` | 关键词断点续抓（默认下载+分析） | 是 |
| `crawl-user <id> --max-pages N [--resume] [--no-analyze]` | 用户全部笔记断点续抓（默认下载+分析） | 是 |
| `crawl-feed --category <cat> --max-pages N [--resume] [--no-analyze]` | 推荐流断点续抓（默认下载+分析） | 是 |
| `analyze-video <id> [--mode <mode>] [--whisper-model <m>] [--frame-interval N] [--step extract transcribe ocr summary]` | 视频内容智能分析（**单条约 2-5 分钟，timeout 需 >=300s**；或改用 crawl 命令） | 否 |
| `setup-video [--mode <mode>] [--whisper-model <m>] [--frame-interval N]` | 交互式配置视频分析 | 否 |
| `analyze-images <id> [--mode <mode>] [--backend <b>] [--no-mermaid] [--step ocr vision mermaid]` | 图片内容智能分析（OCR+AI视觉+Mermaid图表）；`--step` 分段执行 | 否 |
| `setup-image [--mode <mode>] [--backend <b>] [--no-mermaid]` | 交互式配置图片分析 | 否 |
| `setup-wizard` | 统一引导向导：配置图片+视频分析（推荐首次运行） | 否 |
| `export --format md --note <id>` | 单篇 MD（多文件目录：index.md + video.md + images.md + comments.md） | 否 |
| `export --format csv [--user <id>]` | CSV（按博主分文件，`--user` 过滤指定博主） | 否 |
| `export --format json [--user <id>]` | JSON（`--user` 过滤指定博主） | 否 |
| `export --format xlsx [--user <id>]` | XLSX（多 sheet: notes/users/comments，`--user` 过滤指定博主，需 openpyxl） | 否 |
| `accounts` | 多账号状态查看 | 否 |
| `stats [--hours N] [--account <alias>]` | 请求统计（`--account` 按账号过滤） | 否 |
| `refresh-cookies [--force]` | 批量检查并刷新所有账号 cookie | 否 |
| `keepalive [--daemon] [--force] [--account <alias>]` | Cookie 保活（单次或守护进程模式） | 否 |
| `crawl-parallel --users uid1 uid2 ... [--max-pages N]` | 多账号并行爬取不同用户 | 是 |
| `crawl-parallel --keywords kw1 kw2 ... [--max-pages N]` | 多账号并行搜索不同关键词 | 是 |
| `update-js [--dry-run]` | 从 Spider_XHS 拉取最新签名 JS | 否 |
| `analyze --type {sentiment\|topics} [--keyword <kw>] [--user <id>] [--note <id>] [--output json]` | 评论情感分析 / 话题聚类（`--output json` 输出 JSON） | 否 |
| `health` | 系统健康检查（依赖+签名+账号+DB），返回码 0=健康 1=降级 2=严重 | 否 |
| `refresh --max-age-hours N --limit N` | 重抓超过 N 小时的旧笔记（增量更新） | 是 |
| `cleanup [--dry-run] [--vacuum]` | 数据清理：孤儿媒体、过期缓存、VACUUM 压缩 | 否 |

---

## 用户请求 → 命令映射示例

> **用户**："帮我抓一下这条小红书 https://www.xiaohongshu.com/explore/6603abc123?xsec_token=xxx"

```bash
# 完整单篇处理流程（必须执行所有步骤）
python scripts/xhs.py note 6603abc123 --xsec-token xxx --speed-mode paranoid
python scripts/xhs.py download 6603abc123
python scripts/xhs.py comments 6603abc123
python scripts/xhs.py analyze-images 6603abc123
# [MCP 后端时] AI Agent 用 MCP 视觉工具逐张分析图片 → 写入 DB → 重新渲染
python scripts/xhs.py export --format md --note 6603abc123
```

> **用户**："我想备份博主 5fa8xxx 的所有笔记，导成 csv 给飞书用"

```bash
python scripts/xhs.py user 5fa8xxx --pages 50 --speed-mode slow --download --analyze
python scripts/xhs.py export --format csv --user 5fa8xxx
```

> **用户**："搜'露营装备'前 3 页"

```bash
python scripts/xhs.py search "露营装备" --pages 3 --download --analyze
python scripts/xhs.py export --format csv
```

> **用户**："抓不动了一直 460"

→ 切到 paranoid 并降级签名：
```bash
python scripts/xhs.py sign-test  # 先确认签名档可用性
python scripts/xhs.py user <id> --speed-mode paranoid --sign-mode playwright --download --analyze
```

仍不行 → 跳到「签名 JS 月度更新」节。

> **用户**："看看推荐流有什么好内容"

```bash
python scripts/xhs.py feed --category recommend --pages 2
# feed 不支持 --analyze，需要对单条笔记执行分析
# 或使用 crawl-feed --download --analyze 自动分析
python scripts/xhs.py export --format csv
```

> **用户**："分析一下'露营'关键词下的评论情感"

```bash
python scripts/xhs.py search "露营" --pages 3 --download --analyze
python scripts/xhs.py analyze --type sentiment --keyword "露营"
```

> **用户**："看看这个博主发的内容都有什么话题"

```bash
python scripts/xhs.py user <user_id> --pages 10 --download --analyze
python scripts/xhs.py analyze --type topics --user <user_id>
```

> **用户**："签名 JS 过期了" / "sign-test 全 FAIL"

```bash
python scripts/xhs.py update-js
python scripts/xhs.py sign-test   # 验证
```

> **用户**："帮我检查一下所有账号的 cookie 还有没有效"

```bash
python scripts/xhs.py refresh-cookies
```

---

## 视频内容智能分析

小红书大量博主以视频形式发布内容，视频分析功能可对已入库的视频笔记做深度内容提取：

### 依赖（未安装时自动降级为仅 OCR，但分析步骤必须执行）

| 依赖 | 用途 | 安装 |
|---|---|---|
| ffmpeg | 音频提取 + 关键帧抽帧 | `winget install Gyan.FFmpeg` / `brew install ffmpeg` / `sudo apt install ffmpeg` |
| faster-whisper | 语音转文字（本地，支持中文） | `pip install faster-whisper`（含在 requirements.txt） |
| rapidocr-onnxruntime | 画面文字 OCR（纯 Python，CPU 可跑） | `pip install rapidocr-onnxruntime`（含在 requirements.txt） |

### 配置

```bash
# 交互式配置（推荐首次使用时运行）
python scripts/xhs.py setup-video

# 或直接指定参数
python scripts/xhs.py setup-video --mode local --whisper-model base --frame-interval 5
```

配置文件：`data/video_config.json`

### AI 摘要五档

| 模式 | 说明 | 依赖 |
|---|---|---|
| `none` | 不生成 AI 摘要，仅返回转录+OCR 结构化数据 | 无 |
| `local` | 基于转录文本的本地摘要（jieba 关键词+句子评分） | jieba |
| `ollama` | 调用本地 Ollama 模型（支持多模态） | Ollama + 下载模型 |
| `openai` | 调用 OpenAI GPT-4o API（支持帧图片输入） | API Key |
| `mcp` | MCP 视觉工具（AI Agent 提供，零配置） | MCP 视觉 Server |

### 使用

```bash
# 1. 先抓取并下载视频
python scripts/xhs.py note <video_note_id>
python scripts/xhs.py download <video_note_id>

# 2. 分析视频内容
python scripts/xhs.py analyze-video <video_note_id>

# 指定 AI 摘要模式（覆盖配置文件）
python scripts/xhs.py analyze-video <video_note_id> --mode ollama
```

### 视频分析耗时预期（重要）

默认配置针对 **2-5 分钟内的稳定分析** 优化：
- **Whisper 模型**: 默认 `base`（加载 ~10s，精度与速度平衡），追求速度可改为 `tiny`（快 5 倍但精度低）
- **最长转录**: 默认只转录前 300 秒（5 分钟）音频，长视频自动截断
- **典型耗时**: 短视频 60-90s，中等视频 90-180s

> **执行要求**：运行 `analyze-video` 时，终端 timeout 设置 **300 秒（5分钟）** 以覆盖绝大多数视频。长视频（>10分钟）如需完整转录，用 `--max-duration 0` 并设置更长 timeout。含 `--download` 的批量命令 timeout 建议 600 秒。

```bash
# 默认：base 模型 + 前 5 分钟音频（推荐，90-180s 完成）
python scripts/xhs.py analyze-video <note_id>

# 追求速度（精度略降，耗时减半）
python scripts/xhs.py analyze-video <note_id> --whisper-model tiny

# 长视频完整转录（可能需要 5-10 分钟）
python scripts/xhs.py analyze-video <note_id> --max-duration 0
```

**推荐方式一：crawl 命令（不受终端 timeout 限制）**

crawl 命令在单进程内完成所有操作，agent 只需等待最终结果：

```bash
# 推荐：crawl 命令默认下载+分析，内部调度不受 timeout 干扰
python scripts/xhs.py crawl-user <user_id> --max-pages 2
python scripts/xhs.py crawl-search "关键词" --max-pages 3
```

**推荐方式二：单独 analyze-video + 充足 timeout**

```bash
# agent 应设置 timeout >= 300s
python scripts/xhs.py analyze-video <note_id>
```

**方式三：--step 分段执行（仅当 timeout 无法调整时）**

```bash
# 第1步：提取音频+关键帧（约 10-20s）
python scripts/xhs.py analyze-video <note_id> --step extract

# 第2步：语音转录（约 30-120s，设置 timeout>=300s）
python scripts/xhs.py analyze-video <note_id> --step transcribe

# 第3步：OCR + 摘要（约 10-20s）
python scripts/xhs.py analyze-video <note_id> --step ocr summary
```

中间结果缓存在视频目录的 `_cache/` 下，超时后下次调用自动复用。

### 长任务自动分析

crawl 命令**默认开启下载+分析**（`--download` 和 `--analyze` 默认为 True）。如需跳过可用 `--no-download` / `--no-analyze`。

```bash
# 默认行为：自动下载 + 自动分析
python scripts/xhs.py crawl-search "美食" --max-pages 5

# 跳过分析，仅入库+下载
python scripts/xhs.py crawl-user <user_id> --max-pages 20 --no-analyze
```

### 输出

分析结果存入 DB（`video_transcript` / `video_ocr_text` / `video_summary` 字段），自动反映到 CSV 导出和 Markdown 渲染中。

### MCP 视频分析工作流

当 `summary_mode` 为 `"mcp"` 时，Python 脚本完成语音转文字 + 关键帧 OCR 后，输出结构化任务清单，AI Agent 使用 MCP 视觉工具完成摘要。

**第一步：运行分析命令**
```bash
python scripts/xhs.py analyze-video <note_id>
```

**第二步：解析 `[MCP_VIDEO_TASK]` 输出**

脚本在 stderr 输出任务清单：
```
[MCP_VIDEO_TASK]
transcript_length: 1500
keyframe_count: 8
transcript: |
  语音转文字的完整内容...
ocr_text: |
  [frame_0001] 画面中的文字...
keyframes:
  - data/media/博主名/笔记标题/frames/frame_0001.jpg
  ...
prompt: |
  请根据以上视频转录和画面文字信息，生成摘要...
[/MCP_VIDEO_TASK]
```

**第三步：使用 MCP 工具分析视频**

根据当前环境可用的 MCP 视觉工具：
- `analyze_video` → 直接分析视频文件（如 ai-vision-mcp）
- `analyze_image` → 逐帧分析关键帧图片
- 综合转录文字 + 画面分析生成摘要

**第四步：写入 DB**
```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
import xhs_storage, sqlite3
conn = sqlite3.connect('data/xhs.db')
xhs_storage.update_video_analysis(conn, '<note_id>', '<transcript>', '<ocr_text>', '<summary>')
conn.close()
"
```

**第五步：重新渲染 MD**
```bash
python scripts/xhs.py export --note <note_id> --format md
```

---

## 图片内容智能分析

小红书大量关键信息以图片形式呈现（路线图、步骤流程、穿搭分解、参数表格等）。图片分析功能可对已入库的图文笔记做深度内容提取：

### 三层分析能力

| 层级 | 功能 | 说明 |
|---|---|---|
| Layer 1: OCR | 文字提取 | rapidocr 识别图片中所有文字 |
| Layer 2: AI 视觉 | 内容描述 | AI "看懂"图片（路线、穿搭、步骤...） |
| Layer 3: Mermaid | 图表生成 | 自动生成路线图/流程图（嵌入 MD） |

### 依赖（未安装时自动降级为仅 OCR，但分析步骤必须执行）

| 依赖 | 用途 | 安装 |
|---|---|---|
| rapidocr-onnxruntime | 图片文字 OCR | `pip install rapidocr-onnxruntime`（含在 requirements.txt） |
| jieba | 本地模式文本分析 | `pip install jieba`（含在 requirements.txt） |

### 配置

```bash
# 交互式配置（推荐首次使用时运行）
python scripts/xhs.py setup-image

# 或直接指定参数
python scripts/xhs.py setup-image --mode vision --backend api --no-mermaid
```

配置文件：`data/image_config.json`

三维度独立配置：

| 维度 | 选项 | 说明 |
|---|---|---|
| `image_mode` | `auto` / `none` / `local` / `vision` | 自动判断 → 仅 OCR → 本地分析 → AI 视觉（默认 auto） |
| `image_vision_backend` | `ollama` / `api` / `mcp` | 仅 vision 模式 |
| `image_mermaid` | `true` / `false` | 是否生成路线图/流程图 |

视觉后端支持任意 OpenAI 兼容服务商（智谱 GLM-4V / 通义 Qwen-VL / 硅基流动 / DeepSeek / GPT-4o 等）。

MCP 视觉后端（`mcp`）由 AI Agent 运行时提供，支持 Claude Code / GLM Coding / Cursor / Windsurf / Cline 等所有 MCP 客户端。无需额外配置，Python 输出任务清单，Agent 调用自己环境中的 MCP 视觉工具完成分析。

### 使用

```bash
# 1. 先抓取并下载图片
python scripts/xhs.py note <note_id>
python scripts/xhs.py download <note_id>

# 2. 分析图片内容
python scripts/xhs.py analyze-images <note_id>

# 指定模式（覆盖配置文件）
python scripts/xhs.py analyze-images <note_id> --mode vision
python scripts/xhs.py analyze-images <note_id> --mode none  # 仅 OCR
python scripts/xhs.py analyze-images <note_id> --no-mermaid  # 不生成图表
```

### 长任务自动分析

crawl 命令的 `--analyze` 标志已扩展支持图文笔记：

```bash
# 搜索 + 自动下载 + 自动图片分析
python scripts/xhs.py crawl-search "露营" --max-pages 5 --download --analyze
```

### 输出

分析结果存入 DB（`image_ocr_text` / `image_summary` / `image_mermaid` 字段），自动反映到 CSV 导出和 Markdown 渲染中。

### MCP 视觉分析工作流

当 `image_vision_backend` 为 `"mcp"` 时，Python 脚本输出结构化任务清单，AI Agent 使用当前环境中可用的 MCP 视觉工具完成分析。适配所有 MCP 客户端（Claude Code / GLM Coding / Cursor / Windsurf / Cline）。

**第一步：运行分析命令**
```bash
python scripts/xhs.py analyze-images <note_id>
```

**第二步：解析 `[MCP_VISION_TASK]` 输出**

脚本在 stderr 输出任务清单：
```
[MCP_VISION_TASK]
title: 笔记标题
image_count: 5
images:
  - data/media/博主名/笔记标题/img_01.jpg
  - data/media/博主名/笔记标题/img_02.jpg
  ...
ocr_text: |
  [img_01] 已有的 OCR 文字...
prompt: |
  请分析这篇小红书笔记的图片...
[/MCP_VISION_TASK]
```

**第三步：使用 MCP 工具分析图片**

根据当前环境可用的 MCP 视觉工具调用分析：
- 有 `analyze_image` → 用 analyze_image 逐张或分批分析
- 有 `locate_objects` → 可用于目标检测类场景
- 有 `compare_images` → 可分批比较后综合

图片传递方式（根据 MCP Server 支持的输入类型选择）：
- 支持 URL → 先用 Read 工具读取图片（自动上传 CDN），传 CDN URL
- 支持文件路径 → 直接传本地图片路径
- 支持 base64 → 将图片 base64 编码后传入

**第四步：综合所有图片分析结果**

汇总各图片分析结果，生成笔记整体视觉描述（200-500 字中文）。

**第五步：写入 DB**
```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
import xhs_storage, sqlite3
conn = sqlite3.connect('data/xhs.db')
xhs_storage.update_image_analysis(conn, '<note_id>', '<ocr_text>', '<vision_summary>', '')
conn.close()
"
```

**第六步：重新渲染 MD**
```bash
python scripts/xhs.py export --note <note_id> --format md
```

**各 MCP Server 适配指南**

| MCP Server | 后端模型 | 接受输入 | 关键工具 |
|---|---|---|---|
| ai-vision-mcp | Google Gemini / Vertex AI | URL + 文件路径 + base64 | `analyze_image`, `analyze_video` |
| groundlight/mcp-vision | HuggingFace CV | URL + 文件路径 | `locate_objects`, `zoom_to_object` |
| Z.AI Vision MCP | 内置 | 远程 URL | `analyze_image` |
| AI Image MCP | OpenAI Vision | URL + base64 | `analyze_image` |
| OpenRouter Image MCP | OpenRouter | URL | `analyze_image` |

MCP 协议标准：JSON-RPC（`tools/list` 发现 → `tools/call` 调用），stdio/HTTP 双传输。

---

## 错误处理与故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `sign-test` 全 FAIL | JS 文件过期或缺失 / 没装 Node + crypto-js | 1) 先确认 `cd assets && npm install crypto-js`；2) 若仍失败再去更新 JS（见下文「签名 JS 月度更新」） |
| `login` 报 `BrowserNotFound` 或 rookiepy 装不上 | Python 3.13+ 或本地浏览器未登录 | 改 `--prefer qr` 或 `--prefer manual`；最快路径：浏览器 DevTools 复制 cookie 后 `--prefer manual` 粘贴 |
| `[FETCH] 460 风控 ×3` 连续触发 | 短时请求太多 | 自动会切到 Playwright 接管，无需干预；可再加 `--speed-mode paranoid` |
| `[FETCH] success=False code=-100` | cookie 过期 | 自动会重新登录并重试，无需干预 |
| `[FATAL] 达到单账号日抓硬上限 500` | 当日量超出 | 等明天，或换号 |
| `[FATAL] 风控强度超出处理能力` | 三档全失效 + 浏览器接管也失败 | 等 24h；考虑换 IP；最坏情况换账号 |
| Playwright 在 WSL/Linux 启动失败 | 缺系统库 | Linux/WSL: `sudo apt install libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2`；macOS/Windows 一般无需额外安装 |
| `py_mini_racer` 装不上（ARM Mac 等） | wheel 不匹配 | `pip install PyExecJS` + 安装 Node.js（代码会自动 fallback） |

---

## 签名 JS 月度更新（关键运维）

小红书签名 JS 约**每月轮换一次**。判断信号：
1. `sign-test` 三档全 FAIL
2. 抓取大量 460 即使切到 paranoid
3. 上次更新 JS 已超过 30 天

**方式一：自动更新（推荐）**

```bash
python scripts/xhs.py update-js
```

**方式二：手动更新**

```bash
# 1. 拉最新签名 JS（cv-cat/Spider_XHS 月度维护）
git clone --depth 1 https://github.com/cv-cat/Spider_XHS.git /tmp/xhs_ref_latest

# 2. 覆盖到 assets/（mtime 变化后下一次 sign 自动 reload，无需重启）
#    找到日期最新的 xhs_main_*.js
cp /tmp/xhs_ref_latest/static/xhs_main_<日期>.js  assets/xhs_main.js
cp /tmp/xhs_ref_latest/static/xhs_rap.js           assets/

# 3. 验证
python scripts/xhs.py sign-test
```

---

## 单篇笔记完整处理流程（强制标准）

> **重要**：所有笔记必须按以下完整流程处理，确保输出格式统一。每篇笔记的 Markdown 输出必须包含以下五个部分：正文、图片（本地路径）、图片分析（AI 描述 + OCR）、评论。

### 必须执行的步骤（按顺序）

```
步骤 1: 抓取笔记详情
  python scripts/xhs.py note <note_id>

步骤 2: 下载图片/视频到本地
  python scripts/xhs.py download <note_id>

步骤 3: 抓取评论
  python scripts/xhs.py comments <note_id>

步骤 4: 内容智能分析（必须执行，根据类型分流）
  # 图文笔记：
  python scripts/xhs.py analyze-images <note_id>
  # 视频笔记：
  python scripts/xhs.py analyze-video <note_id>

步骤 5: [MCP 后端时] AI Agent 用 MCP 视觉工具完成分析
  → 图文：逐张分析图片 → 综合描述 → 写入 DB
  → 视频：综合转录+OCR+帧画面 → 生成摘要 → 写入 DB

步骤 6: 重新导出 Markdown（最终渲染，包含所有内容）
  python scripts/xhs.py export --note <note_id> --format md
```

**不允许省略任何步骤。** 步骤 4 是必须执行的——即便依赖不完整，至少会产出 OCR 结果。如果某步骤失败，应排查重试，而非跳过。

### 输出 Markdown 必须包含的结构

每篇笔记导出为**多文件目录**：`output/<博主名>/<标题>_<note_id前8位>/`

| 文件 | 内容 | 生成条件 |
|------|------|----------|
| `index.md` | 元数据 + 正文 + 图片 + 视频基本信息 + 子文件链接 | 必有 |
| `video.md` | 视频摘要 + 语音转录 + 画面文字 | 有视频分析结果时 |
| `images.md` | AI 描述 + 图片 OCR + Mermaid 图表 | 有图片分析结果时 |
| `comments.md` | 全部主评论 + 子评论 | 有评论时 |

#### index.md 结构

```markdown
# 笔记标题

- **作者**: 昵称 (@user_id)
- **发布时间**: YYYY-MM-DD HH:MM:SS
- **IP属地**: xxx
- **互动**: 赞 N | 藏 N | 评 N | 分享 N
- **话题**: #tag1 #tag2
- **类型**: normal / video
- **笔记链接**: https://www.xiaohongshu.com/explore/<note_id>

## 正文

...（完整正文内容，保留原始格式）

## 图片

![笔记标题·图1](..\..\..\media\博主名\笔记标题\img_01.jpg)
![笔记标题·图2](..\..\..\media\博主名\笔记标题\img_02.jpg)
...

## 视频（仅视频笔记）

![封面](cover_ref)
- **时长**: 02:30
[视频链接](video_url)
- **本地文件**: `video.mp4`

---

- [视频分析](video.md)
- [图片分析](images.md)
- [评论](comments.md) (N 条)
```

### 关键格式要求

| 项目 | 要求 | 说明 |
|------|------|------|
| **目录结构** | `output/<博主名>/<标题>_<id前8位>/` | 每篇笔记一个子目录，包含 index.md + 可选子文件 |
| **图片路径** | 必须使用本地相对路径 | `![标题·图1](..\..\..\media\博主名\笔记标题\img_01.jpg)`，不使用远程 URL |
| **图片 alt 文本** | `![笔记标题·图N](...)` | 带「笔记标题·」前缀，便于 AI 理解图片上下文 |
| **AI 描述** | images.md 中必须包含 | 每张图一段中文描述 + 末尾【图片总览】整体概括 |
| **OCR 文字** | images.md / video.md 中包含 | OCR 结果按图片/帧去重整理 |
| **评论** | comments.md 全量输出 | 主评论 + 子评论，子评论用 2 空格缩进（`  - `），无数量上限 |
| **评论格式** | `- **昵称** (IP属地, 赞 N): 内容` | 统一格式，IP 属地未知时显示 `?` |
| **正文** | 保留原始格式 | 包括 emoji、换行、话题标签等 |
| **媒体目录** | `data/media/<博主名>/<笔记标题>/` | 按博主名 + 笔记标题双层目录组织 |

### MCP 视觉分析（image_vision_backend 为 mcp 时）

当配置为 MCP 后端时，`analyze-images` 命令只完成 OCR，AI 视觉描述由 AI Agent 执行：

1. **逐张分析**：用 Read 工具上传图片到 CDN → 用 MCP 视觉工具（如 `analyze_image`）分析
2. **避免限速**：MCP 并发请求不超过 3-4 个，遇到 429/500 错误需重试
3. **综合描述**：所有图片分析完成后，汇总为统一格式的中文描述
4. **写入 DB**：`update_image_analysis(conn, note_id, ocr_text, vision_summary, '')`
5. **重新渲染**：`python scripts/xhs.py export --note <note_id> --format md`

AI 描述格式规范：
- 每张图片一行：`图N：场景描述内容。`
- 末尾加【图片总览】：整体概括本组图片的内容和主题
- 描述应包含：地点、场景、人物活动、关键细节
- 使用中文，每张图描述 50-150 字

---

## 输出格式

### 文件命名

| 文件类型 | 命名规则 | 示例 |
|---|---|---|
| Markdown 目录 | `output/<博主名>/<标题>_<note_id前8位>/` | `output/小红/露营装备推荐_6603abc1/` |
| index.md | `index.md`（目录内） | 上述目录下的主文件 |
| video.md | `video.md`（目录内，有视频分析时） | 视频摘要 + 转录 + OCR |
| images.md | `images.md`（目录内，有图片分析时） | AI 描述 + OCR + Mermaid |
| comments.md | `comments.md`（目录内，有评论时） | 全部主评论 + 子评论 |
| CSV | `output/<博主名>/<博主名>_笔记列表.csv` | `output/小红/小红_笔记列表.csv` |
| JSON | `output/xhs_export_{YYYYMMDD_HHMMSS}.json` 或 `output/<博主名>/<博主名>_笔记.json` | 全量或按博主 |
| 媒体 | `data/media/<博主名>/<笔记标题>/img_01.jpg` | `data/media/小红/露营装备推荐/img_01.jpg` |

无博主名时 MD 目录退化到 `output/` 下；无标题时退化为 `{note_id}/`。

### Markdown（单篇笔记，多文件目录）

> **注意**：完整格式规范见上方「单篇笔记完整处理流程（强制标准）」节。以下为标准模板。

导出后生成目录结构：

```
output/
└── 小红/
    └── 露营装备推荐_6603abc1/
        ├── index.md        # 必有
        ├── video.md         # 有视频分析时
        ├── images.md        # 有图片分析时
        └── comments.md      # 有评论时
```

#### index.md

```markdown
# 笔记标题

- **作者**: 昵称 (@user_id)
- **发布时间**: YYYY-MM-DD HH:MM:SS
- **IP属地**: 上海
- **互动**: 赞 1234 | 藏 567 | 评 89 | 分享 12
- **话题**: #tag1 #tag2
- **类型**: normal / video
- **笔记链接**: https://www.xiaohongshu.com/explore/<note_id>

## 正文

...（保留原始格式和 emoji）

## 图片

![笔记标题·图1](..\..\..\media\博主名\笔记标题\img_01.jpg)
![笔记标题·图2](..\..\..\media\博主名\笔记标题\img_02.jpg)
...

## 视频（仅视频笔记）

![封面](cover_ref)
- **时长**: 02:30
[视频链接](video_url)
- **本地文件**: `video.mp4`

---

- [视频分析](video.md)
- [图片分析](images.md)
- [评论](comments.md) (89 条)
```

#### video.md（有视频分析时生成）

```markdown
# 笔记标题 — 视频分析

## 摘要

AI 生成的视频内容摘要...

## 语音转录

语音转文字的完整内容...

## 画面文字

- 第1帧 OCR 文字
- 第2帧 OCR 文字
...
```

#### images.md（有图片分析时生成）

```markdown
# 笔记标题 — 图片分析

## AI 描述

图1：场景描述内容...
图2：场景描述内容...
...
【图片总览】整体概括本组图片...

## 图片文字

[img_01] OCR 提取的文字
[img_02] OCR 提取的文字
...

## 路线图 / 流程图（可选，仅旅游攻略类笔记）

```mermaid
graph LR
    A[起点] -->|交通方式| B[终点]
```
```

#### comments.md（有评论时生成）

```markdown
# 笔记标题 — 评论

共 89 条主评论 + 45 条回复

- **用户A** (上海, 赞 12): 评论内容
  - **用户B** (?, 赞 3): 回复内容
- **用户C** (北京, 赞 8): 评论内容
...
```

**图片 alt 文本**：`![标题·图N](...)` 带「标题·」前缀，方便 AI Agent 理解图片上下文。
**图片路径**：必须使用本地相对路径（`..\..\..\media\博主名\笔记标题\img_01.jpg`），已下载时不用远程 URL。
**子评论缩进**：2 空格标准嵌套列表（`  - `），兼容所有 Markdown 渲染器。
**评论无上限**：comments.md 输出全量评论，不截断。

### CSV（按博主分文件，飞书多维表格导入格式）

按博主分文件到 `output/<博主名>/<博主名>_笔记列表.csv`。可用 `--user <user_id>` 过滤指定博主。

14 列：序号、标题、作者、发布时间、类型、点赞、收藏、评论、分享、IP属地、话题、笔记链接、媒体、正文摘要。UTF-8 BOM + QUOTE_ALL，飞书可直接导入。

**媒体列**：显示已下载的媒体状态（如 "视频+3张图"），无下载时显示 "—"。
**正文摘要**：截取正文前 200 字。

### 状态通知格式

每次操作后自动输出状态摘要，格式：

```
[OK] 《笔记标题》(图文) | 已下载: 3 张图片 | 视频分析: 语音转录 1200 字、AI 摘要 | 图片分析: 图片OCR 500 字、AI 描述 | 5 条评论
```

### 自动下载行为

`note` 命令入库后会**自动下载图片**（仅图片，不下视频）到 `data/media/<博主名>/<笔记标题>/`。视频需用户主动执行 `download` 命令。

---

## 反风控关键机制（自动启用，无需配置）

| 机制 | 说明 | 控制 |
|---|---|---|
| **curl_cffi Chrome TLS 模拟** | 模仿 Chrome 131 的 JA3/JA4 指纹，绕过 TLS 层识别 | `IMPERSONATE_PROFILE`（默认 chrome131） |
| **Session warmup** | 抓正事前先调 `/api/sns/web/v1/homefeed`，模拟"打开首页"的真实导航 | `Fetcher.warmup()` 首次调用自动触发 |
| **xsec_token 全链路透传** | search/user 接口返回的 token 自动入库 → 抓 detail 时自动带上（**直访 detail 无 token = 几乎必触发 461**） | `notes.xsec_token` 字段 |
| **Smart-pacing (burst+rest)** | 不是均匀延迟：连发 3-6 个 → 停 20-60s → 再来一波 — 模仿真人浏览节奏 | `SPEED_PROFILES.normal/slow/paranoid` |
| **周期 cookie 刷新** | 每 20 次抓取调一次 `/api/sns/web/v2/user/me`，让 `websectiga`/`sec_poison_id` 自然更新 | `COOKIE_REFRESH_EVERY=20` |
| **完整 Chrome 请求头集** | sec-ch-ua / sec-fetch-* 全套，与真实 Edge 一致 | `_base_headers()` |
| **响应 Set-Cookie 同步** | 服务端动态更新的 cookie 不丢 | 在 `_call_raw` 自动 |
| **浏览器接管退路** | 连续 3 次 460 / 任一 461 自动切 Playwright 接管；返回非 JSON（验证页）时弹窗让用户 60s 过滑块 | `PlaywrightTakeover` |

## 实测稳定性

测试环境：Edge 131 + `curl_cffi/chrome131` + `--speed-mode normal`：

| 场景 | 触发率 | 耗时 |
|---|---|---|
| search "露营" 1 页（20 条） | 0% | ~10s |
| 连抓 5 条 detail（不同 note） | 0% | ~65s |

**建议持续负载**（保守值）：
- `--speed-mode normal`：< 200 条/小时
- `--speed-mode slow`：< 500 条/天
- `--speed-mode paranoid`：< 1000 条/天（夜跑）

## 进阶反风控（按需）

当 normal 仍触发 461 时考虑：

1. **降速**：`--speed-mode slow` 或 `paranoid`
2. **用代理**：`--proxy http://...`（强烈推荐住宅代理，数据中心 IP 易被标记）
3. **多账号轮换**：每个账号绑独立 `a1` + 独立代理 IP；单账号日抓硬上限 500
4. **JS 签名升级**：若全档持续 460，去 cv-cat/Spider_XHS 拉最新 `xhs_main_<日期>.js` 覆盖（见下文）
5. **改 IMPERSONATE_PROFILE**：换 `chrome120` / `safari17` 等其他指纹（防止 chrome131 被专项标记）

---

## 安全与合规

- **强烈建议小号**：账号封禁不可逆
- **单账号日抓硬上限 500 条**（DAILY_HARD_CAP 写死在代码里）
- **Cookie 落盘 `data/cookies.json`，权限 0600**
- **仅供个人学习研究**，禁止商业用途、禁止转售、禁止大规模数据收集
- **遵守 robots.txt 与平台 ToS**

---

## 当前功能边界

| 阶段 | 状态 | 包含 |
|---|---|---|
| P1 MVP | ✅ 完成 | login / sign-test / note / user / search / export |
| P1.5 反风控 | ✅ 完成 | curl_cffi Chrome TLS / warmup / xsec_token 透传 / smart-pacing / cookie 刷新 |
| **P2 强化** | ✅ **完成** | **comments 评论树 / download 媒体本地化 / crawl-search & crawl-user 断点续抓 / 浏览器接管 JSON 修复** |
| **P3 扩展** | ✅ **完成** | **feed 推荐流 / refresh-cookies token 自动刷新 / analyze 情感分析+话题聚类 / update-js 自动更新 / accounts+stats 子命令** |
| **P4 视频** | ✅ **完成** | **视频内容智能分析：语音转文字（faster-whisper）+ 关键帧 OCR（rapidocr）+ 5 档 AI 摘要（none/local/ollama/openai/mcp）+ 视频流式下载 + DB 视频字段 + CSV/MD 视频增强** |
| **P5 图片** | ✅ **完成** | **图片智能分析：OCR 文字提取（rapidocr）+ AI 视觉描述（Ollama/OpenAI兼容API/MCP视觉工具）+ Mermaid 路线图/流程图 + DB 图片字段 + CSV/MD 图片分析段落 + 批量 crawl 自动触发** |

---

## 项目结构

```
xiaohongshu_scraper_skill/
├── SKILL.md                 # 本文件
├── README.md                # 快速开始文档
├── TECHNICAL_REPORT.md      # 完整技术报告
├── requirements.txt
├── scripts/
│   ├── xhs.py               # CLI 入口 + 命令调度
│   ├── xhs_config.py        # 统一配置 + 路径 + 共享工具 + 指纹池
│   ├── xhs_fetcher.py       # Fetcher 核心类（TLS+节流+风控+浏览器接管）
│   ├── xhs_api.py           # API 调用层 + 数据标准化
│   ├── xhs_media.py         # 媒体下载 + 后处理编排
│   ├── xhs_sign.py          # 三档签名引擎 + auto 路由
│   ├── xhs_login.py         # 跨平台多档登录 fallback（rookiepy/原生提取/QR/手动）
│   ├── xhs_login_native.py  # 跨平台浏览器 Cookie 提取（Windows/macOS/Linux）
│   ├── xhs_login_wsl.py     # WSL 环境 CDP 桥接登录
│   ├── xhs_storage.py       # SQLite 存储 + Markdown/CSV 渲染
│   ├── xhs_accounts.py      # 多账号池（LRU 轮换 + 冷却 + 指纹绑定）
│   ├── xhs_proxy.py         # 代理池（轮换 + 指数冷却）
│   ├── xhs_log.py           # 结构化 JSONL 请求日志
│   ├── xhs_analyze.py       # 评论情感分析 + 话题聚类
│   ├── xhs_update_js.py     # 签名 JS 自动更新（从 GitHub）
│   ├── xhs_video.py         # 视频分析（语音转写 + OCR + AI 摘要）
│   ├── xhs_image.py         # 图片分析（OCR + AI 视觉 + Mermaid）
│   └── xhs_bootstrap.py     # 依赖自动安装
├── assets/                  # 签名 JS 资产
│   ├── xhs_main.js          # 主签名算法（社区维护，约月度轮换）
│   ├── xhs_rap.js           # x-rap-param 签名
│   ├── xhs_xray.js          # x-xray-traceid 签名
│   └── crypto-js.min.js     # CryptoJS（mini-racer 路径用）
├── tests/                   # 测试套件（pytest）
│   ├── conftest.py           # 共享 fixture
│   ├── test_normalize.py     # API 数据标准化测试
│   ├── test_storage.py       # SQLite 存储测试
│   ├── test_accounts.py      # 多账号管理测试
│   ├── test_proxy.py         # 代理池测试
│   └── test_image.py         # 图片分析测试
└── data/                     # 运行时数据（自动生成）
    ├── accounts/<alias>.json # 多账号 Cookie 文件
    ├── accounts_state.json   # 账号运行时状态
    ├── cookies.json           # 单账号 Cookie（兼容）
    ├── xhs.db                # SQLite 数据库
    ├── runs.jsonl             # 请求日志
    ├── media/                # 媒体文件：<博主名>/<笔记标题>/
    └── output/               # MD（按博主/笔记子目录）+ CSV（按博主分文件）
```

---

## 参考项目（开源声明）

- [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) — 签名 JS 核心来源
- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) — Playwright 搭桥范式
- [jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli) — Python 签名参考

## 许可证

MIT
