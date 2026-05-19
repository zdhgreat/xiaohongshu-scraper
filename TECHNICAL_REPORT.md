# 小红书爬虫技术报告

> 更新日期：2026-05-19 | 项目：xiaohongshu_scraper_skill

---

## 一、项目架构总览

```
xiaohongshu_scraper_skill/
├── scripts/                    # Python 主代码
│   ├── xhs.py                  # CLI 入口（argparse + 23 个子命令）
│   ├── xhs_config.py           # 统一配置 + 路径常量 + 指纹池
│   ├── xhs_fetcher.py          # HTTP 核心层（TLS + 节流 + 风控处理 + 浏览器接管）
│   ├── xhs_sign.py             # 三档签名引擎 + auto 路由
│   ├── xhs_accounts.py         # 多账号池（LRU 轮换 + 冷却 + 指纹绑定）
│   ├── xhs_proxy.py            # 代理池（轮换 + 指数冷却）
│   ├── xhs_login.py            # 五档登录 fallback
│   ├── xhs_api.py              # API 调用层 + 数据标准化
│   ├── xhs_storage.py          # SQLite 存储 + Markdown/CSV 渲染
│   ├── xhs_media.py            # 媒体下载 + 后处理编排
│   ├── xhs_image.py            # 图片分析（OCR + AI 视觉 + Mermaid）
│   ├── xhs_video.py            # 视频分析（语音转写 + 关键帧 OCR + AI 摘要）
│   ├── xhs_analyze.py          # 评论情感分析 + 话题聚类
│   ├── xhs_log.py              # 结构化 JSONL 请求日志
│   ├── xhs_update_js.py        # 签名 JS 自动更新（从 GitHub）
│   ├── xhs_bootstrap.py        # 依赖自动安装
│   ├── xhs_login_native.py     # Windows/macOS 原生浏览器 Cookie 提取
│   └── xhs_login_wsl.py        # WSL 环境浏览器 Cookie 提取
├── assets/                      # 签名 JS 资产
│   ├── xhs_main.js             # 主签名算法（社区维护，约月度轮换）
│   ├── xhs_rap.js              # x-rap-param 签名
│   ├── xhs_xray.js             # x-xray-traceid 签名
│   └── crypto-js.min.js        # CryptoJS（mini-racer 路径用）
└── data/                        # 运行时数据（自动生成）
    ├── accounts/<alias>.json   # 多账号 Cookie 文件
    ├── accounts_state.json     # 账号运行时状态
    ├── cookies.json             # 单账号 Cookie（兼容）
    ├── proxies.txt              # 代理列表
    ├── xhs.db                  # SQLite 数据库
    ├── runs.jsonl               # 请求日志
    ├── output/                  # 导出的 Markdown/CSV
    └── media/                   # 下载的图片/视频
```

---

## 二、反检测能力清单（24 项）

### 网络层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 1 | **TLS 指纹模拟** | curl_cffi chrome131 模拟 Chrome 的 JA3/JA4 指纹 | `xhs_fetcher.py:32-38,74` |
| 2 | **完整 Chrome 请求头集** | sec-ch-ua / sec-fetch-* / accept-language 全套 | `xhs_config.py:162-177` |
| 3 | **代理池 + 指数冷却** | 失败后 5→10→20→40→60min 指数退避 | `xhs_proxy.py:47-53` |

### 签名层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 4 | **三档签名引擎** | EmbedJS（py_mini_racer/execjs）→ Playwright → PyPort | `xhs_sign.py:172-348` |
| 5 | **Auto 路由 + 自动降级** | 连续 3 次失败自动切下一档，每 50 次尝试恢复 | `xhs_sign.py:364-446` |
| 6 | **签名 JS 热更新** | 启动时检测文件 mtime 变化自动重载 | `xhs_sign.py:191-206` |
| 7 | **JS 过期预警** | 文件超 30 天警告，超 60 天强警告 | `xhs_sign.py:58-69` |
| 8 | **GitHub 自动更新** | 从 cv-cat/Spider_XHS 拉取最新签名 JS | `xhs_update_js.py:22-88` |

### 行为层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 9 | **Session Warmup** | 首次请求前调 homefeed 模拟打开首页 | `xhs_fetcher.py:153-168` |
| 10 | **Burst+Rest 智能节拍** | normal/slow/paranoid 三档 burst 模型 | `xhs_fetcher.py:208-222` |
| 11 | **自适应降速** | 首次 460 自动 normal→slow→paranoid | `xhs_fetcher.py:173-183` |
| 12 | **IP 级滑动窗口限速** | 同 IP 10 次/60s 硬上限 | `xhs_fetcher.py:188-203` |
| 13 | **长周期休息** | 每 N 次请求强制休息 5-40 分钟 | `xhs_fetcher.py:219-222` |

### 会话层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 14 | **Cookie 周期刷新** | 每 20 次请求调 user/me 刷新 websectiga 等 | `xhs_fetcher.py:227-233` |
| 15 | **Set-Cookie 同步回写** | 每次响应自动同步服务端更新的 cookie | `xhs_fetcher.py:304-311` |
| 16 | **Cookie 启动预检** | 启动时在线验证所有账号，无效的 24h 冷却 | `xhs.py:108-128` |

### 账号层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 17 | **多账号 LRU 轮换** | 最久未用优先，日上限 500/号 | `xhs_accounts.py:180-192` |
| 18 | **460/461 冷却机制** | 460→30min、461→120min 冷却 | `xhs_accounts.py:86-96` |
| 19 | **设备指纹多样化** | 每账号独立 UA/sec-ch-ua/impersonate（MD5 哈希分配） | `xhs_config.py:94-159` |

### 风控层

| # | 技术 | 实现方式 | 文件位置 |
|---|------|---------|---------|
| 20 | **460 处理链** | 首次降速 → 3次轮换代理+账号 → 单号终止 | `xhs_fetcher.py:372-398` |
| 21 | **461 浏览器接管** | Playwright 打开 Chromium 让用户手动过验证码 | `xhs_fetcher.py:400-408,467-541` |
| 22 | **429 指数退避** | 最多 5 次重试，间隔 60-180s | `xhs_fetcher.py:410-416` |
| 23 | **-100 自动重登** | Cookie 失效时自动重新登录（含多账号 profile 隔离） | `xhs_fetcher.py:354-367` |
| 24 | **日抓硬上限** | 单账号 500 次/天，达到即停 | `xhs_config.py:34` |

---

## 三、核心模块详解

### 3.1 Fetcher（xhs_fetcher.py）

请求处理流水线：

```
CLI 命令
  └→ _make_fetcher(args)
       ├→ AccountManager 加载账号 + 分配指纹
       ├→ _validate_accounts() 启动预检
       ├→ ProxyPool 加载代理
       ├→ make_signer() 创建签名器
       └→ Fetcher 构造（应用指纹 + base_headers）

  └→ fetcher.get/post(api)
       └→ _call(method, api, params, data)
            ├→ 日上限检查（500/号）
            ├→ warmup()            # 仅一次
            ├→ _throttle()
            │    ├→ _check_per_ip_rate()    # IP 级限速
            │    └→ burst + rest 节拍
            ├→ _call_raw()
            │    ├→ 构造 URL + body
            │    ├→ signer.sign() → x-s / x-t / x-s-common
            │    ├→ session.get/post（curl_cffi TLS 模拟）
            │    ├→ Set-Cookie 同步
            │    ├→ 日志记录
            │    └→ _handle() 错误处理
            │         ├→ 200: 解析 JSON，-100 重登
            │         ├→ 460: 降速→轮换代理+账号
            │         ├→ 461: 轮换账号→浏览器接管
            │         ├→ 429: 指数退避重试
            │         └→ 403: 退避重试
            └→ _maybe_refresh_cookies()   # 每 20 次
```

**关键状态追踪**：
- `consecutive_460`：连续 460 计数，用于触发降速和轮换
- `_total_460_retries`：全局 460 重试计数，超 20 次终止
- `_ip_timestamps`：每 IP 的滑动窗口时间戳队列
- `burst_remaining`：当前 burst 剩余请求数

### 3.2 签名引擎（xhs_sign.py）

三层架构 + Auto 路由：

```
AutoSigner
  ├→ EmbedJsSigner（首选）
  │    引擎: execjs（Node.js）> py_mini_racer
  │    文件: assets/xhs_main.js / xhs_rap.js / xhs_xray.js
  │    特点: 快速，但 JS 算法约月度轮换需更新
  │
  ├→ PlaywrightSigner（备选）
  │    方式: 真实 Chromium 跑 window._webmsxyw
  │    特点: 无需维护算法，最稳定，但启动慢
  │
  └→ PyPortSigner（占位）
       状态: 有意不实现，作为降级链终点
```

**降级策略**：连续 3 次签名失败 → 切下一档。每 50 次请求尝试恢复到首选。

**过期预警**：`check_js_staleness()` 在 EmbedJsSigner 初始化时检查 xhs_main.js 文件年龄：
- \>30 天：警告，建议更新
- \>60 天：强警告，签名大概率失效

### 3.3 账号管理（xhs_accounts.py）

```
Account 数据结构:
  alias: str              # 账号别名（文件名）
  cookies_path: Path      # Cookie 文件路径
  cookies: dict           # Cookie 数据
  fingerprint: Any        # FingerprintProfile（独立设备指纹）
  last_used: float        # 上次使用时间戳
  cooldown_until: float   # 冷却截止时间
  daily_count: int        # 今日已用次数
  daily_date: str         # 今日日期（自动重置）
  last_460_count: int     # 累计 460 次数
  last_461_count: int     # 累计 461 次数
  total_calls: int        # 累计调用次数

状态持久化: data/accounts_state.json
Cookie 存储: data/accounts/<alias>.json（权限 0600）
```

**轮换策略**：`next_available()` 按 `last_used` 升序排列（LRU），取第一个不在冷却且未达日上限的账号。

### 3.4 设备指纹系统（xhs_config.py）

5 个预置指纹配置：

| 索引 | 浏览器 | 平台 | UA 特征 |
|------|--------|------|---------|
| 0 | Edge 131 | Windows | Chrome/131 Edg/131 |
| 1 | Chrome 131 | Windows | Chrome/131 |
| 2 | Chrome 131 | Mac | Mac OS X 10_15_7 |
| 3 | Edge 131 | Mac | Mac OS X 10_15_7 Edg/131 |
| 4 | Chrome 131 | Windows | Chrome/131 (alt sec-ch-ua) |

**分配算法**：`MD5(alias) % 5`，保证同一账号跨 session 指纹一致。

**应用位置**：
- Fetcher 初始化时应用
- 账号轮换时重新应用
- Cookie 验证时使用对应指纹的 UA

### 3.5 登录系统（xhs_login.py）

五档 fallback 链（auto 模式）：

```
1. win-edge       Windows 原生 Edge Cookie 提取
2. win-chrome     Windows 原生 Chrome Cookie 提取
3. rookie          rookiepy 库提取浏览器 Cookie
4. wsl-edge-cdp   WSL 通过 CDP 协议连接 Windows Edge
5. wsl-edge        WSL 直接访问 Windows Edge Cookie
6. wsl-chrome-cdp  WSL 通过 CDP 协议连接 Windows Chrome
7. wsl-chrome      WSL 直接访问 Windows Chrome Cookie
8. qr              Playwright 扫码登录（每账号独立 profile）
9. manual          手动粘贴 Cookie
```

平台感知：自动检测 WSL/Windows/macOS/Linux，跳过不适用的档位。

**Playwright QR 登录隔离**：通过 `profile_hint` 参数，每个账号使用独立浏览器配置目录（`data/pw_profile_<alias>`），避免多账号登录冲突。

### 3.6 IP 限速（xhs_fetcher.py）

滑动窗口算法：
- 默认：同一 IP 10 次请求 / 60 秒
- 数据结构：`dict[str, deque[float]]`，key 为代理 URL 或 "direct"
- 实现：每次请求前清除过期时间戳，若窗口内请求数 ≥ 上限则等待
- 与 burst+rest 模型叠加：IP 限速是硬上限，burst 节拍是行为模拟

### 3.7 自适应降速（xhs_fetcher.py）

```
触发条件: consecutive_460 == 1（第一次收到 460）

降速路径:
  normal  → slow（连发 3-6 变 2-4，间隔 2.5-5.5s 变 5-12s）
  slow    → paranoid（连发 2-4 变 1-2，间隔 5-12s 变 15-30s）
  paranoid → paranoid（已是最低，不降）

特点: 单向降速，session 内不自动恢复。用户需重启才能重置。
```

---

## 四、数据存储

### 4.1 SQLite Schema（xhs_storage.py）

5 张核心表：

| 表名 | 主要字段 | 用途 |
|------|---------|------|
| `notes` | note_id, title, description, type, user_id, xsec_token, like_count, image_analysis, video_analysis | 笔记主表（22列） |
| `users` | user_id, nickname, avatar, fans_count, note_count | 用户信息（9列） |
| `comments` | comment_id, note_id, content, user_name, like_count, sub_comment_count | 评论（13列） |
| `search_cache` | keyword, page, note_ids | 搜索缓存 |
| `crawl_state` | task_id, task_type, target_id, cursor, status | 抓取进度状态 |

### 4.2 文件存储

| 路径 | 内容 | 格式 |
|------|------|------|
| `data/accounts/<alias>.json` | 账号 Cookie | JSON dict |
| `data/accounts_state.json` | 运行时状态 | JSON |
| `data/runs.jsonl` | 请求日志 | JSON Lines（自动轮转，50MB/文件，最多 3 个） |
| `data/media/<博主>/<标题>/` | 下载的图片/视频 | jpg/mp4 |
| `data/output/` | 导出文件 | md/csv |

---

## 五、媒体分析能力

### 5.1 图片分析（xhs_image.py）

三层分析流水线：

```
Layer 1: OCR 文字提取
  引擎: rapidocr-onnxruntime
  输出: 图片中的文字内容

Layer 2: AI 视觉描述
  后端: api（OpenAI 兼容）/ ollama / mcp
  流程: OCR 结果 → 判断是否需要视觉分析 → 分批上传 → AI 描述 → 合成总结

Layer 3: Mermaid 图表
  条件: 图片含数据/流程/对比类内容
  生成: AI 生成 Mermaid 语法 + 语法校验
```

### 5.2 视频分析（xhs_video.py）

五档摘要模式：

| 模式 | 依赖 | 能力 |
|------|------|------|
| `none` | 无 | 仅提取语音转写文本 |
| `local` | jieba | 本地关键词提取摘要 |
| `ollama` | Ollama | AI 摘要（支持视觉模型分析关键帧） |
| `openai` | OpenAI API | AI 摘要（支持视觉模型） |
| `mcp` | AI Agent | 通过 MCP 视觉工具分析关键帧 |

处理流程：`ffmpeg 提取音频 → faster-whisper 语音转写 → ffmpeg 提取关键帧 → OCR 关键帧文字 → AI 生成摘要`

---

## 六、配置体系

### 6.1 代码内默认值（xhs_config.py）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DAILY_HARD_CAP` | 500 | 单账号日抓上限 |
| `COOKIE_REFRESH_EVERY` | 20 | 每 N 次请求刷新 Cookie |
| `IMPERSONATE_PROFILE` | "chrome131" | curl_cffi TLS 指纹版本 |
| `IP_RATE_LIMIT` | 10 | 同 IP 每窗口最大请求数 |
| `IP_RATE_WINDOW` | 60 | IP 限速窗口（秒） |

### 6.2 外部配置覆盖（data/config.json）

可选，覆盖代码内默认值。支持：
- `daily_hard_cap` / `cookie_refresh_every` / `impersonate_profile`
- `user_agent` / `sec_ch_ua`
- `speed_profiles.{name}.burst_size/gap/rest_gap/...`

### 6.3 模块级配置文件

| 文件 | 模块 | 内容 |
|------|------|------|
| `data/image_config.json` | xhs_image | 图片分析后端配置 |
| `data/video_config.json` | xhs_video | 视频分析模式配置 |

---

## 七、CLI 命令体系（23 个子命令）

| 类别 | 命令 | 功能 |
|------|------|------|
| **账号** | `login` | 多档登录（10 种 --prefer 选项） |
| | `refresh-cookies` | 批量刷新/验证 Cookie |
| | `stats` | 账号状态查看 |
| **数据抓取** | `search` | 关键词搜索笔记 |
| | `note` | 获取单篇笔记详情 |
| | `user` | 获取用户信息 |
| | `user-notes` | 获取用户发布的笔记 |
| | `feed` | 获取推荐流（6 个分类） |
| | `comments` | 获取笔记评论（含子评论） |
| **媒体处理** | `download` | 下载笔记媒体（--no-video / --overwrite） |
| | `analyze-images` | 图片分析（OCR + AI + Mermaid） |
| | `analyze-video` | 视频分析（转写 + OCR + 摘要） |
| **分析** | `analyze` | 评论情感分析 + 话题聚类 |
| **导出** | `export` | 导出为 Markdown |
| | `export-csv` | 导出为 CSV |
| **系统** | `sign-test` | 签名引擎测试 |
| | `update-js` | 更新签名 JS |
| | `setup` | 依赖安装 |
| | `setup-wizard` | 交互式配置向导 |
| | `setup-image` | 图片分析配置 |
| | `setup-video` | 视频分析配置 |

通用参数：`--speed-mode` / `--sign-mode` / `--account` / `--proxy`

---

## 八、风险矩阵与安全建议

### 当前 2-3 号 + 无代理风险评估

```
风险等级: ★★☆☆☆（低）

理由：
✅ TLS 指纹模拟 — 不被 JA3/JA4 检测
✅ 完整请求签名 — 三档引擎 + 自动降级
✅ 行为节拍 — burst+rest + 自适应降速 + IP 限速
✅ 设备指纹隔离 — 每号独立 UA/sec-ch-ua
✅ Cookie 管理 — 预检 + 周期刷新 + 自动重登
✅ 日抓上限 — 500/号，在平台容忍范围内

⚠️ 剩余风险：同 IP 关联
  - 2-3 号相当于一家人各刷各的，平台通常不干预
  - 5+ 号建议加代理（住宅 IP），每号绑独立 IP
```

### 已知缺口（需代理或外部服务解决）

| 缺口 | 严重性 | 解决方案 | 成本 |
|------|--------|---------|------|
| 账号-代理未绑定 | 高（5+号时） | 代码改动 + 住宅代理 | ¥50-200/月 |
| Playwright 指纹伪装 | 低（仅过验证码时） | playwright-stealth | 免费 |
| 静态资源模拟 | 低 | Warmup 增强资源请求 | 免费 |

---

## 九、已完成改进记录

> 2026-05-19 实施的 5 项免费反检测改进 + 1 项 bug 修复

### 9.1 已完成改进

| # | 改进 | 问题 | 方案 | 涉及文件 |
|---|------|------|------|---------|
| 1 | 自适应降速 | speed_mode 静态，首次 460 不降速 | consecutive_460==1 时自动 normal→slow→paranoid | xhs_config.py, xhs_fetcher.py |
| 2 | Cookie 启动预检 | 用过期 cookie 发请求白白触发风控 | 启动时在线验证，无效的 24h 冷却 | xhs_accounts.py, xhs.py |
| 3 | IP 级请求计数 | 同 IP 多号总频率无上限 | 滑动窗口 10 次/60s 硬限 | xhs_config.py, xhs_fetcher.py |
| 4 | 签名 JS 过期预警 | JS 文件几个月没更新无提示 | 超 30 天警告，超 60 天强警告 | xhs_sign.py |
| 5 | 设备指纹多样化 | 多号共用同一 UA/sec-ch-ua | 5 个指纹池，MD5(alias) 确定性分配 | xhs_config.py, xhs_accounts.py, xhs_fetcher.py, xhs_login.py, xhs.py |

### 9.2 Bug 修复

`xhs_fetcher.py:_handle()` 方法缺少 `count` 参数，导致 `-100` cookie 失效重登路径抛出 `NameError`。已修复：签名加 `count: bool = True`，调用处传 `count=count`。

---

## 十、未来改进计划

### P0 — 短期（纯代码，无外部依赖）

#### 10.1 账号-代理绑定

**当前问题**：账号轮换和代理轮换是独立的。同 IP 多号被关联是最大封号风险。

```
现状:
  account1 → proxy_A → 460 → 切账号
  account2 → proxy_A ← 还是同一个 IP！

目标:
  account1 → proxy_1（绑定的专属代理）
  account2 → proxy_2（绑定的专属代理）
```

**实现方案**：
- `xhs_accounts.py` 的 `Account` 添加 `proxy_url: str | None` 字段
- `xhs_proxy.py` 添加 `get_bound(url: str) -> Proxy | None` 方法
- `xhs_fetcher.py` 的 `_rotate_account()` 中同时切换到新账号的绑定代理
- `data/accounts_state.json` 持久化绑定关系
- 未绑定代理的账号走直连（向后兼容）

**预计改动**：~100 行，涉及 xhs_accounts.py / xhs_proxy.py / xhs_fetcher.py

**无代理时的效果**：无代理时该改进无实际效果，但代码结构已就绪，一旦引入代理即可生效。

---

#### 10.2 代理地理位置一致性检查

**当前问题**：用上海代理 IP 但 UA/时区没有任何地域匹配检查。

**实现方案**：
- 指纹池扩展：每个 FingerprintProfile 添加 `timezone: str` 和 `region: str` 字段
- 代理 IP 归属地查询（可用的免费 API：ip-api.com）
- 启动时或代理切换时检查 IP 归属地与当前指纹的 region 是否矛盾
- 矛盾时输出警告或自动切换到匹配的指纹

**预计改动**：~40 行，涉及 xhs_config.py / xhs_fetcher.py

---

#### 10.3 签名版本号追踪

**当前问题**：只检查文件年龄，不知道签名算法是否已被平台更新废弃。

**实现方案**：
- `xhs_update_js.py` 更新时记录 JS 文件的 git commit hash 到 `data/js_version.json`
- `check_js_staleness()` 同时检查版本记录
- 可选：启动时发送一个探测请求验证签名是否有效

**预计改动**：~30 行，涉及 xhs_sign.py / xhs_update_js.py

---

### P1 — 中期（需要少量外部依赖）

#### 10.4 Playwright 指纹伪装

**当前问题**：过验证码时启动的 Playwright 只有 `--disable-blink-features=AutomationControlled`（2019 年技术），Canvas/WebGL/AudioContext 指纹都是默认值。

**实现方案**：
- 方案 A：集成 `playwright-stealth`（pip install playwright-stealth）
- 方案 B：自定义 JS 注入脚本覆盖 Canvas/WebGL/AudioContext 指纹
- 方案 C：使用 `camoufox`（基于 Firefox 的反检测浏览器）

**关键原则**：一致性 > 随机性
- Windows UA + Windows 字体 + Win32 platform
- 中文语言 + Asia/Shanghai 时区 + 中国 IP
- 同 session 内指纹固定，跨 session 可变化

**预计改动**：~100 行，涉及 xhs_fetcher.py（PlaywrightTakeover） / 新增 JS 注入脚本

**影响范围**：仅 Playwright 浏览器路径（过验证码 + PlaywrightSigner），curl_cffi 纯 HTTP 路径不受影响。

---

#### 10.5 住宅代理 API 对接

**当前问题**：当前代理池只支持手动维护 `data/proxies.txt`。数据中心 IP 几分钟内就会被小红书封禁，必须用住宅/移动 IP。

**推荐代理服务商**：

| 服务商 | 类型 | 价格 | 特点 |
|--------|------|------|------|
| Bright Data | 住宅/移动 | $3-15/GB | 全球最大代理池，API 完善 |
| Oxylabs | 住宅 | $2-10/GB | 企业级稳定性 |
| Smartproxy | 住宅 | $1-5/GB | 性价比高 |
| IPRoyal | 住宅 | $1.5-5/GB | 支持粘性会话 |
| 360Proxy | 住宅 | ¥50-200/月 | 国内服务商，中文支持 |

**实现方案**：
- `xhs_proxy.py` 添加动态代理 API 支持
- 配置 `data/config.json` 新增 `proxy_api` 字段：`{url, key, type: "bright-data"|"oxylabs"|...}`
- 启动时或代理耗尽时从 API 拉取新代理
- 配合 P0 账号-代理绑定使用

**预计改动**：~120 行，涉及 xhs_proxy.py / xhs_config.py

---

#### 10.6 Warmup 增强（静态资源模拟）

**当前问题**：Warmup 只调 1 个 API，不加载 CSS/JS/图片。真实浏览器访问首页会产生 30+ 个资源请求。

**实现方案**：
- 使用 Playwright 拦截资源请求，记录真实访问的资源 URL 列表
- 在 curl_cffi 路径中，warmup 后额外请求 3-5 个关键静态资源（favicon、main.js、logo.png）
- 随机化请求顺序和间隔

**预计改动**：~60 行，涉及 xhs_fetcher.py

---

### P2 — 长期（需要架构调整或外部服务）

#### 10.7 验证码自动解决

**当前问题**：461 验证码需要用户手动在 Chromium 窗口里过。多账号高频抓取时手动过码不可持续。

**实现方案**：
- 集成第三方验证码解决服务
  - CapSolver：支持小红书滑块/图形验证，$1-3/1000 次
  - 2Captcha：老牌服务，支持多种验证码类型
- 流程：检测到 461 → 截图 → 发送到 CapSolver API → 获取解决方案 → 自动执行
- 保留手动模式作为 fallback

**预计改动**：~150 行，新增 xhs_captcha.py 模块

---

#### 10.8 反检测浏览器集成

**当前问题**：Playwright 默认浏览器的自动化特征仍可被高级检测工具（如 CreepJS）识别。

**商业反检测浏览器对比**：

| 产品 | 指纹管理 | 价格 | 中文支持 | API |
|------|---------|------|---------|-----|
| AdsPower | 多维度指纹隔离 | ~$36/月 | 优秀 | 有 |
| BitBrowser | Chromium 内核 | ~$30/月 | 优秀 | 有 |
| GoLogin | Orbula 引擎 | ~$49/月 | 一般 | 有 |
| Multilogin X | 企业级 | ~$99/月 | 一般 | 有 |
| Dolphin Anty | 免费层可用 | 免费-$89/月 | 一般 | 有 |

**推荐方案**：
- 优先考虑 AdsPower 或 BitBrowser（中文支持好、价格低）
- 通过其 CLI API 启动浏览器实例，替代当前的 Playwright 直接启动
- 每个账号在反检测浏览器中创建独立 Profile，指纹完全隔离

**预计改动**：~200 行，涉及 xhs_fetcher.py / xhs_sign.py / 新增 xhs_anti_detect.py

**注意**：此改进仅对 Playwright 路径（过验证码 + PlaywrightSigner）有意义。curl_cffi 纯 HTTP 路径不需要。

---

#### 10.9 行为模拟增强

**当前问题**：请求间隔虽然用了 burst+rest 模型，但请求序列的模式仍较机械（总是 API 调用，从不访问页面）。

**实现方案**：
- 在 burst 之间随机插入"浏览行为"请求：访问笔记详情页 HTML（而非 API）、用户主页
- 偶尔触发"回看"行为：重复访问之前看过的内容
- 模拟搜索行为：偶尔发一个搜索请求，但不抓取结果
- 请求间隔分布从均匀随机改为对数正态分布（更接近真实用户行为）

**预计改动**：~80 行，涉及 xhs_fetcher.py

---

### 改进优先级总览

```
┌──────────────────────────────────────────────────────────────────┐
│ 现在就能做（免费，纯代码）                                         │
│                                                                    │
│  □ P0.1 账号-代理绑定           ~100 行   5+号时必需               │
│  □ P0.2 地理位置一致性           ~40 行   避免时区/IP 矛盾         │
│  □ P0.3 签名版本追踪             ~30 行   提前发现签名失效         │
├──────────────────────────────────────────────────────────────────┤
│ 短期可做（需少量外部依赖）                                         │
│                                                                    │
│  □ P1.1 Playwright 指纹伪装     ~100 行   playwright-stealth 免费 │
│  □ P1.2 住宅代理 API 对接       ~120 行   ¥50-200/月              │
│  □ P1.3 Warmup 增强              ~60 行   模拟静态资源请求         │
├──────────────────────────────────────────────────────────────────┤
│ 长期规划（需要架构调整或付费服务）                                  │
│                                                                    │
│  □ P2.1 验证码自动解决           ~150 行   $1-3/1000次             │
│  □ P2.2 反检测浏览器集成         ~200 行   ¥30-100/月              │
│  □ P2.3 行为模拟增强              ~80 行   纯代码                  │
└──────────────────────────────────────────────────────────────────┘
```

### 旧手机热点方案（免费平替住宅代理）

如果有闲置的旧手机，可以用 4G 热点替代付费住宅代理：

```
手机1 (4G热点) → 账号1（独立移动 IP，最难被检测）
手机2 (4G热点) → 账号2（独立移动 IP）
家宽带直连     → 账号3（住宅 IP，正常使用）

优势：4G/5G IP 本身就是移动住宅 IP，比买的代理还难被检测
成本：0（已有手机 + SIM 卡）
限制：每部手机一个 IP，需要手动开关热点
```

配合账号-代理绑定（P0.1）使用，每个账号绑定对应手机热点的 IP。

---

## 十一、版本历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-05-19 | v1.5 | 5 项免费反检测改进 + _handle count bug 修复 |
| 2026-05-18 | v1.4 | 多账号支持（login --name / accounts 目录 / LRU 轮换） |
| 2026-05-17 | v1.3 | 视频分析五档模式 + MCP 视觉支持 |
| 2026-05-16 | v1.2 | 图片分析三层架构 + Mermaid 图表 |
| 2026-05-15 | v1.1 | 统一配置 xhs_config.py + 外部 config.json 覆盖 |
| 2026-05-14 | v1.0 | 初始版本：Fetcher 拆分 + 签名引擎 + 代理池 |
