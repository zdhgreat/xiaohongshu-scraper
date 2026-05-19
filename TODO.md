# 小红书爬虫 — 剩余优化任务

> 更新时间：2026-05-19
> 已完成：P0 全部、P1 全部、Phase 1 全部、Phase 2 部分已合并到 P1
> 测试：133 个测试全部通过

---

## Phase 2 剩余（P1）

### ~~Step 7: 合并重复登录模块~~ ✅ 已完成
### ~~Step 8: fetch_session 上下文管理器~~ ✅ 已完成

### Step 9: 统一异常体系
**状态**: 未开始

当前异常分散：`FatalRiskError`、`SignError`、`LoginError`、`AccountError`、裸 `RuntimeError`

引入基础异常层级：
```python
class XhsError(Exception): ...
class RiskControlError(XhsError): ...   # 460/461/429
class AuthError(XhsError): ...          # Cookie/登录
class ConfigError(XhsError): ...        # 配置问题
```

涉及文件：`xhs_fetcher.py`、`xhs_login.py`、`xhs_accounts.py`、`xhs_sign.py`

---

## Phase 3: 数据生命周期 + 功能增强（P2）

### Step 10: 增量更新机制
**状态**: 未开始

- 在 `notes` 表新增 `content_hash` 列（Schema migration via `_migrate()`）
- `_normalize_note()` 计算关键字段（title/description/互动数据）的轻量 hash
- 新增 `xhs.py refresh --max-age-hours 24` 子命令，重抓超过 N 小时的笔记
- 避免每次 search 都重写未变更的数据

### ~~Step 11: 日志轮转~~ ✅ 已完成
- `xhs_log.py` 已有 `_rotate_if_needed()`：50MB 自动轮转，最多 3 个历史文件

### Step 12: 扩展导出格式
**状态**: 未开始

- 在 `xhs_storage.py` 或新建 `xhs_export.py` 中添加：
  - **JSON 导出**（`--format json`）— 最简单，数据已是 dict 形式
  - **Excel 导出**（`--format xlsx`）— 使用 `openpyxl`（可选依赖），多 sheet（notes/users/comments）
- 当前仅支持 `md` 和 `csv`

### Step 13: 数据清理命令
**状态**: 未开始

新增 `xhs.py cleanup [--keep-days 30]` 子命令：
- 清理 DB 中已删除笔记对应的 media 文件
- 清理过期的 `search_cache` 和 `crawl_state` 记录
- 可选 `VACUUM` SQLite 数据库

---

## Phase 4: 质量提升（P3）

### Step 14: 健康检查命令
**状态**: 未开始

新增 `xhs.py health` 子命令：
- 检查所有依赖（curl_cffi/execjs/node/crypto-js/playwright/ffmpeg）
- 验证每个账号 Cookie 是否有效
- 报告签名子系统状态
- 检查 DB 完整性（`PRAGMA integrity_check`）
- 返回码：0=健康，1=降级，2=严重
- 对 Claude Code Skill 场景特别有价值，AI agent 可快速判断爬虫是否就绪

### Step 15: Cookie 安全增强
**状态**: 未开始

在 `xhs_login.py` 的 `load_cookies`/`persist_cookies` 中添加可选 AES-256-GCM 加密：
- 通过环境变量 `XHS_COOKIE_KEY` 或交互输入获取密钥
- `cryptography` 库已是项目依赖

### Step 16: 类型注解补充
**状态**: 未开始

- 运行 `pyright`/`mypy` strict mode
- 补充关键函数的类型注解（`_guess_ext`、`_download_media` 等）
- 添加 `py.typed` 标记文件

### Step 17: JS 签名资产版本检查
**状态**: 未开始

在 `_make_fetcher()` 中添加：
- 本地 JS 文件超过 35 天时自动警告（签名算法月度轮换）
- `xhs_update_js.py` 添加版本比对，跳过无变更的更新

---

## 已完成任务汇总

| 任务 | 阶段 | 状态 |
|------|------|------|
| Step 1: 创建统一配置模块 `xhs_config.py` | Phase 1 | ✅ |
| Step 2: 拆分 Fetcher 到 `xhs_fetcher.py` | Phase 1 | ✅ |
| Step 3: 拆分 API 到 `xhs_api.py`（已合并入 xhs.py） | Phase 1 | ✅ |
| Step 4: 拆分媒体下载到 `xhs_media.py` | Phase 1 | ✅ |
| Step 5: 瘦身 xhs.py 主入口（1784→914行） | Phase 1 | ✅ |
| Step 6: 建立测试框架（133个测试） | Phase 1 | ✅ |
| Step 7: 合并登录模块（删除 xhs_login_win_native.py） | Phase 2 | ✅ |
| Step 8: fetch_session 上下文管理器 | Phase 2 | ✅ |
| Step 11: 日志轮转（50MB，3个历史文件） | Phase 3 | ✅ |
| 24个 CRITICAL/HIGH/MEDIUM 代码级 bug 修复 | — | ✅ |
| P1: bootstrap 副作用修复（ensure_ready 移到 main） | P1 | ✅ |
| P1: cmd_analyze/cmd_setup 等移到子模块 | P1 | ✅ |
