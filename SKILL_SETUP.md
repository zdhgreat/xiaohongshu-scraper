---
name: xiaohongshu-scraper
description: 抓取小红书（RedNote/XHS）笔记、用户、搜索结果，落库 SQLite + Postgres 双写，支持视频分析、图片 OCR、Markdown 导出。五层纵深防御架构应对阿瑞斯风控体系。
metadata:
  openclaw:
    requires:
      bins:
        - node
        - ffmpeg
---

# 小红书爬虫安装指南

## Step 1: 创建虚拟环境

```bash
cd <skill_base_dir>
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

## Step 2: 安装 Python 依赖

```bash
pip install -r requirements.txt
```

核心依赖说明：

| 依赖 | 用途 |
|------|------|
| `curl_cffi` | Chrome TLS 指纹模拟（反风控核心） |
| `PyExecJS` | 签名引擎（需 Node.js 运行 JS 签名代码） |
| `playwright` | 浏览器自动化（登录/签名/浏览器接管/DOM 搜索） |
| `playwright-stealth` | Playwright 反检测增强 |
| `rookiepy` | 浏览器 Cookie 提取（Windows/macOS） |
| `cryptography` | WSL Cookie 解密（AES-GCM） |
| `py-mini-racer` | 备选 JS 引擎（Node.js 不可用时降级） |
| `faster-whisper` | 视频语音转文字（可选） |
| `rapidocr-onnxruntime` | 图片/视频帧 OCR（可选） |
| `jieba` | 中文分词（内容分析） |
| `snownlp` | 中文情感分析 |
| `qrcode` + `pillow` | 二维码登录 |

> **Hub/Postgres 集成**（可选）：需要额外安装 `pip install -r requirements-hub.txt`，包含 `psycopg2-binary`、`python-dotenv`、`financial_hub_postgres`。不安装则仅使用本地 SQLite。

## Step 3: 安装 Node.js + crypto-js

签名引擎（PyExecJS）需要 Node.js 运行时：

```bash
# 检查 Node.js 是否已安装
node --version

# 如未安装（按平台选择）：
#   Windows: winget install OpenJS.NodeJS
#   macOS:   brew install node
#   Linux:   sudo apt install nodejs

# 安装签名 JS 依赖
cd assets && npm install && cd ..
```

## Step 4: 安装 Playwright Chromium

浏览器自动化（登录/签名/DOM 搜索/浏览器接管）需要 Chromium：

```bash
playwright install chromium
```

## Step 5: 配置 .env

复制模板并填写 Postgres 连接信息：

```bash
cp .env.example .env
# 编辑 .env 填入实际的数据库连接参数
```

最小配置（仅 SQLite，不连接 Postgres）：

无需配置 `.env`，爬虫自动使用本地 `data/xhs.db`。Postgres 不可用时自动降级为仅 SQLite。

完整配置（对接 Hub）：

```env
DATABASE_URL=postgresql://user:password@localhost:5432/financial_hub
# 或独立变量：
# POSTGRES_HOST=localhost
# POSTGRES_PORT=5432
# POSTGRES_USER=xhs
# POSTGRES_PASSWORD=your_password
# POSTGRES_DB=financial_hub
```

## Step 6: 验证安装

```bash
# 全面健康检查（依赖 + 签名 + 账号 + 数据库）
python scripts/xhs.py health

# 仅验证签名
python scripts/xhs.py sign-test
```

`health` 返回码：
- `0` = 全部正常
- `1` = 降级运行（部分功能不可用，如 Postgres 未连接）
- `2` = 严重问题（签名不可用等）

## 可选依赖

| 依赖 | 用途 | 安装 |
|------|------|------|
| `ffmpeg` | 视频音频提取 + 关键帧抽帧 | `winget install Gyan.FFmpeg` / `brew install ffmpeg` / `sudo apt install ffmpeg` |
| `playwright` + Chromium | QR 扫码登录 / 浏览器接管 | `playwright install chromium` |
| `rookiepy` | 浏览器 cookie 自动提取（< Python 3.13） | 已含在 requirements.txt |

未安装可选依赖时，对应功能自动降级，不影响核心爬取能力。
