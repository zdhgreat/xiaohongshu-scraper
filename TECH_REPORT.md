# 小红书爬虫 Skill 技术报告

> 基于桌面设计报告《小红书爬虫项目方案书》(v1.0, 2025-05-17) 与实际代码的差异分析，
> 记录方向变更、完成度评估及后续待办。
>
> **注意**：本文件记录设计文档与实现的对比。完整技术文档请参阅 [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md)。

---

## 一、项目概述

| 项目 | 内容 |
|------|------|
| 仓库路径 | `~/workspace/xiaohongshu_scraper_skill` |
| 代码规模 | ~5,800 行 Python（18 个模块） + 4 个 JS 签名资产 |
| 当前版本 | v1.6.0 |
| 完成阶段 | P1 MVP + P1.5 反风控 + P2 强化 + P3 扩展 + P4 视频 + P5 图片 + P6 稳定性（全部闭合） |
| 未启动阶段 | 无 |

---

## 二、方向变更总览

### 2.1 原始设计方向（方案书 v1.0）

- **技术路线**：路线 C "混合模式" — requests 为主 + DrissionPage/Playwright 为 fallback
- **架构假设**：单文件 `scripts/xhs.py`（约 2000 行），所有子命令集中在一个文件
- **数据库**：规划 7 张表（users / notes / comments / images / videos / search_cache / crawl_state）
- **登录方式**：rookiepy 自动提取 → QR 扫码 → 手动粘贴，三档 fallback
- **签名维护**：内置 x-s/x-t 签名算法，预期需频繁维护

### 2.2 实际实现方向

- **技术路线**：`curl_cffi` Chrome TLS 模拟为主 + Playwright 浏览器接管为 fallback，**未采用 DrissionPage**
- **架构结果**：多文件拆分（18 个 Python 模块），主文件 `xhs.py` ~1000 行
- **数据库**：5 张表（users / notes / comments / search_cache / crawl_state），**images/videos 独立表被砍**，改为从 `raw_json` 中按需提取
- **登录方式**：新增 WSL Edge CDP 桥接（`xhs_login_wsl.py`），支持 Windows 宿主浏览器透传；保留 rookiepy/QR/manual 三档
- **签名维护**：改为 **三档签名可降级**（PlaywrightSigner / EmbedJsSigner / PyPortSigner），EmbedJs 基于社区 `cv-cat/Spider_XHS` 的 JS 资产，月度替换文件即可，**无需维护算法本身**

### 2.3 核心变更原因

1. **放弃 DrissionPage**：Playwright 已能满足浏览器接管和 QR 登录，引入 DrissionPage 增加依赖冗余
2. **放弃纯 Python 签名**：小红书签名算法月度轮换，维护成本过高；改为社区 JS 资产 + Playwright 兜底，将维护工作外部化
3. **多文件拆分**：反风控逻辑（账户管理、代理池、日志、签名、存储）复杂度超出单文件可维护范围
4. **砍 images/videos 表**：SQLite 中存大表意义不大，直接从 `raw_json` 提取渲染更灵活

---

## 三、功能完成度对照表

| 功能 | 方案书规划 | 实际状态 | 差异说明 |
|------|-----------|---------|---------|
| `login`（rookiepy 提取） | ✅ 规划 | ✅ 完成 | — |
| `login --qr`（QR 扫码） | ✅ 规划 | ✅ 完成 | Playwright 实现 |
| `login --manual`（手动粘贴） | ✅ 规划 | ✅ 完成 | — |
| `token`（查看/刷新/验证） | ✅ 规划 | ✅ 完成 | `refresh-cookies` 子命令，含在线验证 + 自动重登 |
| `sign-test`（签名健康检查） | — | ✅ 完成 | 新增，实际必要 |
| `note`（单笔记详情） | ✅ 规划 | ✅ 完成 | — |
| `user`（用户主页+笔记列表） | ✅ 规划 | ✅ 完成 | — |
| `search`（关键词搜索） | ✅ 规划 | ✅ 完成 | — |
| `comments`（评论树+子评论） | — | ✅ 完成 | P2 新增，超出原方案书 |
| `download`（图片/视频本地化） | ✅ 规划 | ✅ 完成 | — |
| `export`（MD/CSV） | ✅ 规划 | ✅ 完成 | — |
| `crawl-search`（关键词断点续抓） | — | ✅ 完成 | P2 新增 |
| `crawl-user`（用户笔记断点续抓） | — | ✅ 完成 | P2 新增 |
| `accounts`（多账号状态查看） | — | ✅ 完成 | P2 新增 |
| `stats`（请求统计） | — | ✅ 完成 | P2 新增 |
| `feed`（推荐流/分类流） | ✅ 规划 | ✅ 完成 | 6 个分类 + crawl-feed 断点续抓 |
| `token`（查看/刷新/验证） | ✅ 规划 | ✅ 完成 | `refresh-cookies` 子命令，含在线验证 + 自动重登 |
| `analyze`（情感分析/话题聚类） | — | ✅ 完成 | P3 新增，SnowNLP + jieba，缺失时降级 |
| `update-js`（签名 JS 自动更新） | — | ✅ 完成 | P3 新增，从 Spider_XHS 拉取 |
| 代理池支持 | — | ✅ 完成 | P2 新增 |
| 多账号轮换 | — | ✅ 完成 | P2 新增 |
| 浏览器接管（460/461 fallback） | ✅ 规划 | ✅ 完成 | Playwright 实现 |
| `PyPortSigner`（纯 Python 签名） | — | ⚠️ **占位未实现** | 明确 raise 提示降级 |
| `xhs_xray.js` 完整集成 | — | ⚠️ **部分集成** | x-xray-traceid 已加，x-rap-param 未全面启用 |
| 转录纠错（OCR+LLM） | — | ✅ 完成 | v1.6.0 新增，用 OCR 画面文字纠正 Whisper 转录错误 |
| 心跳保活 | — | ✅ 完成 | v1.6.0 新增，防止 Kimi 2.6 等 60s 静默 kill |
| PID 文件锁 | — | ✅ 完成 | v1.6.0 新增，四层防御解决数据库死锁 |
| 输出按博主分目录 | — | ✅ 完成 | v1.6.0 新增，`output/<博主名>/<标题>.md` |
| 评论精简渲染 | — | ✅ 完成 | v1.6.0 新增，Top 20 主评论 + Top 5 子回复 |

**代码层面已修复项（P3 阶段闭合）：**
- ~~`scripts/xhs.py`：`build_parser()` 中 `p_login` 未暴露 `--name` 参数~~ → 已修复
- ~~`build_parser()` 缺少 `accounts` / `stats` 子命令注册~~ → 已修复
- ~~`_add_common()` 缺少 `--account` 参数~~ → 已修复
- ~~`_handle()` 中 -100 错误处理写回 `cookies.json` 而非当前账号路径~~ → 已修复
- `xhs_sign.py`：`PyPortSigner` 保留为有意降级终点（非 bug）

---

## 四、架构差异详述

### 4.1 项目结构差异

**方案书规划：**
```
xiaohongshu_scraper_skill/
├── SKILL.md
├── README.md
├── requirements.txt
├── .gitignore
└── scripts/
    ├── xhs.py            # 单文件 2000 行
    └── login_qr.py       # 可选辅助
```

**实际结构：**
```
xiaohongshu_scraper_skill/
├── SKILL.md                 # Agent 平台读取的 Skill 配置
├── README.md                # 快速开始文档
├── TECHNICAL_REPORT.md      # 完整技术报告
├── TECH_REPORT.md           # ← 本文件（设计对比）
├── requirements.txt         # Python 依赖
├── .gitignore
├── assets/                  # 签名 JS 资产
│   ├── xhs_main.js          # 签名核心（cv-cat/Spider_XHS）
│   ├── xhs_rap.js           # x-rap-param JSVMP
│   ├── xhs_xray.js          # x-xray-traceid
│   └── crypto-js.min.js     # JS 引擎依赖
├── data/                    # 运行时数据（gitignore）
│   ├── xhs.db               # SQLite
│   ├── xhs.pid              # PID 文件锁（运行时）
│   ├── cookies.json         # 持久化 cookie
│   ├── accounts/            # 多账号 cookie 文件
│   ├── output/              # 导出输出
│   │   ├── <博主名>/        # MD 按博主分目录
│   │   │   └── <标题>_<id>.md
│   │   └── *.csv / *.json   # 批量导出
│   ├── media/               # 下载的媒体文件
│   │   └── <博主名>/<标题>/
│   │       ├── img_*.jpg / video.mp4
│   │       ├── keyframes/   # 视频关键帧（保留）
│   │       └── _cache/      # 临时分析缓存（可清理）
│   └── pw_profile/          # Playwright 浏览器 profile
└── scripts/
    ├── xhs.py               # CLI 入口 + 命令调度（~940 行）
    ├── xhs_config.py        # 统一配置 + 路径 + 指纹池
    ├── xhs_fetcher.py       # HTTP 核心层（TLS+节流+风控+浏览器接管）
    ├── xhs_api.py           # API 调用层 + 数据标准化
    ├── xhs_media.py         # 媒体下载 + 后处理编排
    ├── xhs_sign.py          # 三档签名引擎 + auto 路由
    ├── xhs_login.py         # 五档登录 fallback
    ├── xhs_login_native.py  # Windows/macOS 原生浏览器 Cookie 提取
    ├── xhs_login_wsl.py     # WSL 环境 CDP 桥接登录
    ├── xhs_storage.py       # SQLite 存储 + Markdown/CSV 渲染
    ├── xhs_accounts.py      # 多账号池（LRU 轮换 + 冷却 + 指纹绑定）
    ├── xhs_proxy.py         # 代理池（轮换 + 指数冷却）
    ├── xhs_log.py           # 结构化 JSONL 请求日志
    ├── xhs_analyze.py       # 评论情感分析 + 话题聚类
    ├── xhs_update_js.py     # 签名 JS 自动更新（从 GitHub）
    ├── xhs_video.py         # 视频分析（语音转写 + OCR + AI 摘要）
    ├── xhs_image.py         # 图片分析（OCR + AI 视觉 + Mermaid）
    └── xhs_bootstrap.py     # 依赖自动安装
```

### 4.2 反风控架构差异

| 维度 | 方案书规划 | 实际实现 |
|------|-----------|---------|
| HTTP 层 | requests + headers 轮换 | `curl_cffi` Chrome 131 TLS/JA3/JA4 指纹模拟 |
| 签名层 | 内置算法，需维护 | 社区 JS 资产 + Playwright 兜底 + 纯 Python 占位 |
| 速度控制 | 均匀延迟 3-7s | **burst+rest 模型**：连发 3-6 个 → 停 20-60s |
| Session 模拟 | 无 | **warmup**：抓前先调 homefeed 模拟首页停留 |
| Cookie 更新 | 401 时重登 | **周期刷新**：每 20 次调 user/me 让 websectiga 更新 |
| xsec_token | 未提及 | **全链路透传**：search/user 拿到的 token 自动入库，detail 自动带 |
| 浏览器接管 | DrissionPage fallback | **Playwright takeover**：460×3 或 461 自动弹窗 |
| 代理支持 | `--proxy` 单代理 | **代理池**：多代理轮换 + 失败冷却 |
| 多账号 | 未提及 | **账号池**：460/461 自动切账号，日抓硬上限 500/账号 |

---

## 五、依赖变更

| 依赖 | 方案书规划 | 实际使用 | 说明 |
|------|-----------|---------|------|
| requests | ✅ | ✅ 兜底 | curl_cffi 不可用时 fallback |
| curl_cffi | ❌ | ✅ 核心 | 反指纹必备，新增 |
| rookiepy | ✅ | ✅ | Python 3.13+ 暂无 wheel，已加版本限定 |
| playwright | ✅ | ✅ | 登录 QR + 浏览器接管 + PlaywrightSigner |
| drissionpage | ✅ | ❌ **未使用** | 方向变更，已放弃 |
| py-mini-racer | ❌ | ✅ 可选 | 备用 JS 引擎 |
| PyExecJS | ❌ | ✅ 核心 | 签名 JS 主引擎 |
| cryptography | ❌ | ✅ | WSL cookie 解密 |
| qrcode + pillow | ❌ | ✅ | QR 登录展示 |

---

## 六、数据库 Schema 变更

**方案书规划 7 张表 → 实际 5 张表**

被砍的表：
- `images` → 合并到 `notes.raw_json`，渲染时按需提取
- `videos` → 同上

新增字段：
- `notes.xsec_token` / `notes.xsec_source`（反风控关键）
- `comments.pictures_json` / `comments.target_comment_id` / `comments.ip_location`

---

## 七、P6 稳定性增强（v1.6.0）

### 7.1 转录纠错管线

**问题**：Whisper tiny 模型对中文同音字和英文术语识别错误严重（"缺德"→"确"、"Ctrl+C"→"康车性"、"Faster"→"Festor"）。

**解决方案**：在视频分析管线的 OCR 之后、摘要之前，插入 LLM 纠错步骤：

```
extract → transcribe → ocr → [correct] → summary
```

纠错函数 `_correct_transcript()` 使用 OCR 画面文字作为地面真相参照，调用已有 LLM 后端（OpenAI/Ollama）纠正转录错误。无 LLM 可用时优雅降级，保留原始转录。

| 配置项 | 变更 |
|--------|------|
| 默认 Whisper 模型 | `tiny` → `base`（WER 从 ~30% 降至 ~15%） |
| OCR 存储 | 纯文本 → JSON 数组（保留帧关联） |
| 纠错缓存 | `_cache/transcript_corrected.json` |
| 原始转录保留 | `_cache/transcript.json`（Whisper 原始输出） |

### 7.2 四层防御解决数据库死锁

**问题**：终端超时后 Python 进程未退出，持续占用 SQLite 写锁，导致后续命令报 `database is locked`。

**解决方案**：

| 层级 | 机制 | 代码位置 |
|------|------|---------|
| 1. PID 文件锁 | `_check_stale_lock()` 检测残留进程并自动清理 | `xhs_storage.py` |
| 2. SIGTERM 处理 | 信号处理器调用 `_release_lock()` 释放 PID 文件 | `xhs.py` |
| 3. busy_timeout | 10s → 30s，增加等待容忍度 | `xhs_storage.py` |
| 4. atexit 清理 | 进程退出时自动释放 PID 文件 | `xhs_storage.py` |

跨平台进程检测：Unix 用 `os.kill(pid, 0)`，Windows 用 `ctypes.windll.kernel32.OpenProcess`。

### 7.3 心跳保活

所有长耗时命令（11 个）启动后台守护线程，每 15s 向 stderr 输出心跳。防止 Kimi 2.6 等 AI 平台的 60s 静默 kill 机制误杀进程。

### 7.4 输出结构优化

| 改进 | 旧 | 新 |
|------|-----|-----|
| MD 目录 | `output/` 平铺 | `output/<博主名>/` 分目录 |
| 文件名 | `{note_id}_{作者}_{标题}.md` | `{标题}_{note_id前8位}.md` |
| 评论渲染 | 全量展开 | Top 20 主评论 + Top 5 子回复 + 省略提示 |
| OCR 渲染 | 纯文本（含重复/噪声） | JSON 解析 + 去重 + 噪声过滤 |
| 摘要标题 | 固定 `### 视频摘要` | 有摘要→`视频摘要`，无摘要→`内容提取结果` |

### 7.5 文件管理

- **关键帧保留**：视频分析完成后保留 `keyframes/` 目录，仅清理 audio.wav 和 JSON 缓存
- **缓存清理重试**：Windows 文件锁兼容，3 次重试间隔 1s
- **旧数据兼容**：纯文本 OCR → `_render_video_ocr()` 自动回退原样输出

---

## 八、已知问题与限制

1. **PyPortSigner 有意不实现**：纯 Python 签名端口作为 auto 降级链的终点保留。不影响使用，因为 auto 模式会 fallback 到 embed-js/playwright。
2. **WSL Playwright 依赖**：需要安装大量系统库（libnss3 等），否则浏览器闪退。
3. **rookiepy Python 3.13+ 不兼容**：无预编译 wheel，需 Rust 工具链或改用 QR/manual。
4. **签名 JS 月度轮换**：`xhs_main.js` 来自社区，约每月失效一次。可用 `update-js` 命令自动更新。
5. **snownlp / jieba 可选依赖**：未安装时情感分析降级为简单词库，话题聚类降级为字符拆分。`pip install snownlp jieba` 即可启用完整功能。
6. **转录纠错依赖 LLM**：无 OpenAI/Ollama 配置时纠错步骤自动跳过，保留 Whisper 原始输出。

---

## 九、后续建议

所有规划阶段（P1-P3）已完成。可选的增强方向：

| 优先级 | 事项 | 说明 |
|--------|------|------|
| 可选 | 评论导出飞书多维表格 | 将评论 + 情感分数导出为飞书导入格式 CSV |
| 可选 | 定时任务集成 | cron / Windows Task Scheduler 定时 crawl + refresh-cookies |
| 可选 | Web UI 看板 | 基于入库数据的简单 Web 展示（Flask/Streamlit） |
| 可选 | 更多 feed 分类 | 当前 6 个分类，可按需从 XHS 网页端抓取更多 category_id |

---

## 十、打包清单

打包路径：`~/.hermes/skills/social-media/xiaohongshu_scraper_skill/`

包含文件：
- SKILL.md（Agent 使用说明，不动）
- README.md（快速开始）
- TECH_REPORT.md（本文件）
- requirements.txt
- skill.yaml
- _meta.json
- .gitignore
- scripts/（18 个 Python 模块）
- assets/（4 个 JS 签名文件 + node_modules/）
- data/（运行时生成，打包时排除）

排除项（.gitignore 已覆盖）：
- `data/xhs.db`
- `data/cookies.json`
- `data/output/`
- `__pycache__/`

---

*报告更新时间：2026-05-20*
*P6 稳定性阶段已闭合，v1.6.0*
*完整技术文档请参阅 TECHNICAL_REPORT.md*
