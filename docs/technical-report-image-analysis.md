# 技术报告：图片智能分析功能设计

> 版本：v1.0 | 日期：2026-05-18 | 状态：设计中

---

## 1. 背景

小红书（RedNote）平台的内容形态以图文笔记为主，大量关键信息以图片形式呈现：

| 内容类型 | 典型场景 | 信息形态 |
|----------|----------|----------|
| 旅游攻略 | 路线图、行程规划、交通示意 | 地理拓扑 + 文字标注 |
| 穿搭分享 | 单品分解、搭配公式、品牌清单 | 视觉关系 + 文字列表 |
| 美食菜谱 | 步骤图解、食材清单、用量标注 | 序列流程 + 数值信息 |
| 工具教程 | 操作截图、参数设置、对比示意 | 步骤序列 + 界面元素 |
| 好物推荐 | 产品对比、参数表格、价格信息 | 表格数据 + 视觉对比 |

当前爬虫仅对图片做下载和路径引用（`![标题·图1](url)`），**图片中的文字、路线、步骤、对比关系等结构化信息完全未被提取**。

### 现有能力盘点

| 能力 | 状态 | 所在模块 |
|------|------|----------|
| 图片下载 | 已有 | `xhs_media.download_media()` |
| 自动下载（轻量） | 已有 | `xhs_media.auto_download_note()` |
| 视频帧 OCR | 已有 | `xhs_video.ocr_frames()` — rapidocr |
| Ollama 视觉调用 | 已有 | `xhs_video.summarize_ollama()` — base64 图片 |
| OpenAI 兼容 API 视觉调用 | 已有 | `xhs_video.summarize_openai()` — Vision API 格式 |
| 笔记图片 OCR | **缺失** | — |
| 笔记图片 AI 视觉理解 | **缺失** | — |
| 路线图/流程图生成 | **缺失** | — |

---

## 2. 功能目标

### 2.1 三层分析能力

```
Layer 1: OCR 文字提取    →  图片中所有可识别的文字
Layer 2: AI 视觉描述     →  AI "看懂"图片内容（路线、穿搭、步骤...）
Layer 3: Mermaid 图表    →  自动生成路线图/流程图（嵌入 MD）
```

### 2.2 灵活配置

用户可根据需求、预算、隐私偏好自由组合：

- 只要文字 → `image_mode: none`（纯 OCR，零 AI 依赖）
- 不联网 → `image_mode: local`（OCR + jieba 本地文本分析）
- 本地 AI → `image_mode: vision` + `image_vision_backend: ollama`
- 云端 AI → `image_mode: vision` + `image_vision_backend: api`
- 不要路线图 → `image_mermaid: false`

---

## 3. 架构设计

### 3.1 模块结构

```
scripts/
├── xhs_image.py    ← 新建：图片分析核心模块
├── xhs_video.py    ← 复用：ocr_frames(), _ollama_supports_vision()
├── xhs_storage.py  ← 修改：DB schema + MD 渲染 + CSV
├── xhs_media.py    ← 修改：post_process_note() 扩展
├── xhs.py          ← 修改：CLI 命令注册
└── xhs_config.py   ← 不变

data/
├── image_config.json  ← 新增：图片分析独立配置
└── video_config.json  ← 不变：视频分析配置
```

### 3.2 配置体系

图片分析采用**三维度独立配置**，而非互斥模式：

```
┌─────────────────────────────────────────────────────┐
│  Dimension 1: image_mode                            │
│  none   →  仅 OCR，不做 AI 分析                     │
│  local  →  OCR + jieba 本地文本分析                 │
│  vision →  OCR + AI 视觉模型看图                    │
│                                                     │
│  Dimension 2: image_vision_backend (仅 vision 模式)  │
│  ollama →  本地 Ollama 视觉模型                     │
│  api    →  远程 OpenAI 兼容 API（通用格式）          │
│                                                     │
│  Dimension 3: image_mermaid                         │
│  true   →  自动检测路线/流程并生成 Mermaid 图表     │
│  false  →  不生成                                   │
└─────────────────────────────────────────────────────┘
```

#### 配置文件 `data/image_config.json`

```json
{
  "image_mode": "vision",
  "image_vision_backend": "api",
  "image_mermaid": true,

  "api_base_url": "https://open.bigmodel.cn/api/paas/v4",
  "api_key": "xxx.xxx",
  "api_model": "glm-4v-plus",

  "ollama_url": "http://localhost:11434",
  "ollama_model": "qwen2-vl:7b"
}
```

#### 默认值

首次无配置文件时默认 `image_mode: "none"`（最安全，零 AI 依赖，仅 OCR）。

#### 视觉后端兼容性

`image_vision_backend: "api"` 模式通过 `api_base_url` 指向任意 OpenAI 兼容服务商：

| 服务商 | base_url | 特点 |
|--------|----------|------|
| 智谱 GLM-4V | `https://open.bigmodel.cn/api/paas/v4` | 中文视觉强，有免费额度 |
| 通义千问 Qwen-VL | 阿里 DashScope | 中文视觉强，有免费额度 |
| 硅基流动 SiliconFlow | `https://api.siliconflow.cn/v1` | 多模型，有免费额度 |
| DeepSeek-VL | `https://api.deepseek.com/v1` | 便宜 |
| OpenAI GPT-4o | `https://api.openai.com/v1` | 效果最好，国内需代理 |
| 月之暗面 Moonshot | `https://api.moonshot.cn/v1` | 暂不支持视觉 |

### 3.3 数据流

```
用户执行 analyze-images 或 crawl --analyze
    │
    ▼
xhs_image.analyze_images(note_id, conn, cfg)
    │
    ├─ 1. find_local_images() ──→ [img_01.jpg, img_02.jpg, ...]
    │
    ├─ 2. OCR 层（始终执行）
    │   └─ xhs_video.ocr_frames(image_paths) ──→ ocr_text
    │
    ├─ 3. 分析层（根据 image_mode）
    │   ├─ none  → 跳过
    │   ├─ local → _analyze_local(ocr_text, title, desc) ──→ image_summary
    │   └─ vision → _vision_ollama/_vision_api(image_paths, prompt, cfg)
    │              ├─ 阶段1: 内容分析 prompt ──→ image_summary
    │              └─ 阶段2: Mermaid prompt ──→ mermaid_code（如开启）
    │
    └─ 4. 返回 {ocr_text, image_summary, mermaid, image_count}
        │
        ▼
xhs_storage.update_image_analysis(conn, note_id, ...)
        │
        ▼
xhs_storage.write_markdown(conn, note_id) ──→ MD 文件含分析段落
```

### 3.4 多图分批策略

小红书图文笔记通常 3-18 张图片。AI 后端有图片数量限制：

| 后端 | 每批上限 | 策略 |
|------|----------|------|
| Ollama `/api/generate` | 5 张 | 分批发送，最后综合 |
| OpenAI 兼容 Chat Completions | 3 张 | 分批发送，最后综合 |

分批+综合流程：
```
9 张图片 + Ollama（5张/批）
    ├─ Batch 1: [img_01..img_05] → partial_summary_1
    ├─ Batch 2: [img_06..img_09] → partial_summary_2
    └─ Synthesis: partial_summary_1 + partial_summary_2 → final_summary
```

综合调用发送文本摘要（不再发图片），让 AI 合并为统一描述。

---

## 4. Prompt 设计

### 4.1 阶段 1 — 内容分析 Prompt

```
你是一个小红书图文内容分析助手。请根据以下图片内容，生成详细的中文内容描述和分析。

请注意：
1. 逐张描述图片中的主要内容（如景点、物品、步骤、穿搭等）
2. 如果是教程/攻略类内容，提取关键步骤和要点
3. 如果有文字信息，请准确转录
4. 总结整个图文笔记的核心价值和关键信息
5. 提取可能被搜索到的关键词

笔记标题：{title}
笔记正文：{description}
图片中识别到的文字（OCR）：
{ocr_text}
```

### 4.2 阶段 2 — Mermaid 生成 Prompt

```
基于以下图片内容分析，判断这篇笔记是否包含路线图、流程图或步骤序列。

如果包含，请生成 Mermaid 语法代码（graph LR 或 flowchart TD），并输出 JSON：
{"has_diagram": true, "mermaid_code": "graph LR\n    ...", "diagram_type": "route"}

diagram_type 取值：route（旅游路线）、steps（教程步骤）、comparison（对比分析）

如果不包含路线/流程/步骤类内容：
{"has_diagram": false}

注意：
- 节点名称用中文，连线标签包含交通方式/时间/费用等关键信息
- 使用 graph LR（路线类）或 flowchart TD（步骤类）
- 保持简洁，节点不超过 10 个

图片内容分析：
{image_summary}
```

### 4.3 本地模式（local）文本分析

不调 AI，仅用 jieba 对 OCR 文字做：
- 关键词提取（TF-IDF 前 20 个）
- 高频词统计
- 与笔记标题/正文的关联分析

输出格式为结构化文本，非 AI 摘要。

---

## 5. Mermaid 图表输出规范

### 5.1 图表类型

| diagram_type | Mermaid 语法 | 适用场景 | 示例 |
|-------------|-------------|---------|------|
| `route` | `graph LR` | 旅游路线、地理拓扑 | 成都→九寨沟→黄龙 |
| `steps` | `flowchart TD` | 教程步骤、操作流程 | 准备→步骤1→步骤2 |
| `comparison` | `graph LR` | 产品对比、方案选择 | 方案A vs 方案B |

### 5.2 渲染示例

```markdown
### 图片分析

#### AI 描述

这是一篇成都-九寨沟-黄龙三日游攻略。Day1 从成都双流机场出发，
乘飞机约1小时抵达九寨沟黄龙机场，入住沟口酒店。Day2 全天游览
九寨沟，推荐路线为树正沟→日则沟→则查洼沟...

#### 图片文字

Day1 成都→九寨沟 飞机1h 住宿：沟口酒店 ¥280/晚
Day2 九寨沟全天 门票¥169 观光车¥90...

#### 路线图

​```mermaid
graph LR
    A[成都双流] -->|飞机1h ¥450| B[九寨沟黄龙机场]
    B -->|大巴1.5h| C[九寨沟沟口]
    C -->|Day2 全天| D[九寨沟景区]
    D -->|大巴3h| E[黄龙景区]
    E -->|飞机1h| A

    style A fill:#f9f,stroke:#333
    style D fill:#bbf,stroke:#333
​```
```

### 5.3 校验规则

Mermaid 代码在写入前做基础校验：
- 必须以 `graph`、`flowchart`、`sequenceDiagram` 开头
- 节点数不超过 15（防止过于复杂）
- 不含非法字符
- 校验失败时静默跳过（不影响 AI 描述输出）

---

## 6. DB Schema 变更

### 6.1 新增列

| 列名 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `image_ocr_text` | TEXT | `''` | 所有图片 OCR 文字合并 |
| `image_summary` | TEXT | `''` | AI 视觉描述 / 本地文本分析 |
| `image_mermaid` | TEXT | `''` | Mermaid 图表代码 |

### 6.2 迁移方式

在 `xhs_storage.py` 的 `_migrate()` 中追加：

```python
if "image_ocr_text" not in cols:
    conn.execute("ALTER TABLE notes ADD COLUMN image_ocr_text TEXT DEFAULT ''")
if "image_summary" not in cols:
    conn.execute("ALTER TABLE notes ADD COLUMN image_summary TEXT DEFAULT ''")
if "image_mermaid" not in cols:
    conn.execute("ALTER TABLE notes ADD COLUMN image_mermaid TEXT DEFAULT ''")
```

采用与现有 `video_*` 列相同的增量迁移模式，老数据库自动兼容。

### 6.3 更新函数

```python
def update_image_analysis(conn, note_id, ocr_text="", summary="", mermaid=""):
    conn.execute(
        "UPDATE notes SET image_ocr_text=?, image_summary=?, image_mermaid=? "
        "WHERE note_id=?",
        (ocr_text, summary, mermaid, note_id),
    )
    conn.commit()
```

### 6.4 CSV 输出

CSV 从 23 列扩展到 26 列，新增：

| 列位置 | 列名 | 内容 |
|--------|------|------|
| 20 | 图片OCR文字 | 所有图片识别文字 |
| 21 | 图片分析摘要 | AI 视觉描述 |
| 22 | 图片Mermaid图 | Mermaid 代码 |

---

## 7. CLI 命令设计

### 7.1 新增命令

#### `analyze-images`

```bash
python scripts/xhs.py analyze-images <note_id> [--mode {none,local,vision}] [--backend {ollama,api}] [--no-mermaid]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `note_id` | 图文笔记 ID（需已入库） | — |
| `--mode` | 覆盖配置文件的分析模式 | 使用配置文件 |
| `--backend` | 覆盖配置文件的视觉后端 | 使用配置文件 |
| `--no-mermaid` | 关闭 Mermaid 图表生成 | 使用配置文件 |

执行流程：
1. 从 DB 读取笔记，验证存在
2. 查找本地图片，不存在则先下载
3. 加载配置（CLI 参数覆盖配置文件）
4. 执行分析（OCR → AI 视觉 → Mermaid）
5. 结果写入 DB
6. 重新渲染 MD 文件
7. 输出状态摘要

#### `setup-image`

```bash
python scripts/xhs.py setup-image [--mode {none,local,vision}] [--backend {ollama,api}] [--no-mermaid]
```

交互引导用户配置：
1. 选择分析模式（none / local / vision）
2. 如选 vision → 选择后端（ollama / api）
3. 如选 api → 输入 base_url、api_key、model
4. 如选 ollama → 输入 url、model
5. 选择是否开启 Mermaid
6. 保存到 `data/image_config.json`

### 7.2 现有命令变更

#### `crawl-search` / `crawl-user` / `crawl-feed` 的 `--analyze` 标志

当前 `--analyze` 仅处理视频笔记。变更后：

```
if do_analyze and is_video:
    # 视频分析（已有逻辑）
elif do_analyze and not is_video:
    # 图文分析（新增逻辑）
    xhs_image.analyze_images(note_id, conn, cfg)
```

---

## 8. 依赖与兼容性

### 8.1 依赖矩阵

| 功能 | 依赖 | 安装方式 | 已在 requirements.txt |
|------|------|----------|----------------------|
| OCR 文字提取 | rapidocr-onnxruntime | `pip install rapidocr-onnxruntime` | 是 |
| 本地文本分析 | jieba | `pip install jieba` | 否（需新增） |
| Ollama 视觉 | Ollama + 视觉模型 | 用户自行安装 | 不需要 |
| API 视觉 | 任意 OpenAI 兼容 API | 用户自行注册 | 不需要 |

### 8.2 降级策略

```
vision 模式 + Ollama 后端
    │
    ├─ Ollama 未启动 → 警告，降级到 local 模式
    ├─ Ollama 无视觉模型 → 警告，降级到 local 模式
    └─ Ollama 正常 → 执行视觉分析

vision 模式 + API 后端
    │
    ├─ API Key 未配置 → 警告，降级到 local 模式
    ├─ API 调用失败 → 警告，降级到 local 模式
    └─ API 正常 → 执行视觉分析

Mermaid 生成失败 → 静默跳过，image_summary 仍保留
```

所有降级不中断主流程，保证 OCR 结果始终可用。

---

## 9. 文件修改清单

| 文件 | 操作 | 改动内容 |
|------|------|----------|
| `scripts/xhs_image.py` | **新建** | 配置管理 + 图片发现 + 分批 + OCR + AI 视觉 + Mermaid + 主入口 |
| `scripts/xhs_storage.py` | 修改 | 3 列 schema 迁移 + `update_image_analysis()` + MD 渲染 + CSV + 状态摘要 |
| `scripts/xhs_media.py` | 修改 | `post_process_note()` 增加 `elif do_analyze and not is_video` 分支 |
| `scripts/xhs.py` | 修改 | `cmd_analyze_images()` + `cmd_setup_image()` + 2 个子命令注册 + 帮助文本 |
| `SKILL.md` | 修改 | 命令表 + MD 模板 + CSV 列数 + 图片分析配置说明 |
| `requirements.txt` | 修改 | 新增 `jieba` 依赖 |

---

## 10. 验证计划

| 测试项 | 命令 | 预期 |
|--------|------|------|
| 配置保存 | `setup-image --mode vision --backend api` | `image_config.json` 正确生成 |
| 纯 OCR | `analyze-images <id> --mode none` | 仅 `image_ocr_text` 有值 |
| 本地分析 | `analyze-images <id> --mode local` | `image_summary` 有 jieba 关键词 |
| AI 视觉 | `analyze-images <id> --mode vision` | `image_summary` 有 AI 描述 |
| 路线图 | 对旅游攻略笔记执行分析 | `image_mermaid` 有 Mermaid 代码 |
| 非路线内容 | 对穿搭笔记执行分析 | `image_mermaid` 为空（不强制生成） |
| MD 渲染 | `export --note <id> --format md` | 包含 `### 图片分析` + mermaid 代码块 |
| CSV 导出 | `export --format csv` | 26 列，图片分析列有值 |
| 批量分析 | `crawl-search "露营" --max-pages 1 --download --analyze` | 图文笔记自动触发图片分析 |
| 降级测试 | Ollama 未启动时执行分析 | 自动降级到 local，不中断 |
| 语法检查 | `py_compile` 所有修改文件 | 全部通过 |
