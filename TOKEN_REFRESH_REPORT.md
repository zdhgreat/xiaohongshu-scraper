# 小红书 Token Refresh 可行性研究报告

> 目标：研究是否可以通过纯 HTTP API（无需浏览器）实现 cookie 自动刷新，
> 彻底消除对 Playwright profile 的依赖，实现 100% 自动化的多账号 cookie 管理。

---

## 一、背景

### 当前方案（Keepalive）
- 依赖 Playwright persistent profile 保存浏览器 session
- 定期 headless 加载 profile 访问小红书，复用 session 获取 cookie
- 缺点：profile 有状态、占用磁盘、浏览器版本升级可能不兼容、首次仍需 QR 扫码

### 理想方案（Token Refresh）
- 纯 HTTP API 调用，不依赖浏览器
- 用旧 cookie/token 换新 cookie/token
- 首次登录也可通过 API 完成（手机号+验证码 / Web QR → API 化）

---

## 二、小红书 Web Cookie 结构分析

小红书 Web 端的关键 cookie：

| Cookie 名 | 作用 | 有效期 | 刷新机制 |
|---|---|---|---|
| `web_session` | 主会话 token，鉴权核心 | 约 7-30 天（服务端控制） | 请求时 Set-Cookie 续期 |
| `a1` | 设备标识 | 长期（约 180 天） | 自动生成 |
| `webId` | 浏览器指纹 ID | 长期 | - |
| `websectiga` | 安全验证 token | 短期（数小时） | 请求时 Set-Cookie 续期 |
| `sec_poison_id` | 风控标记 | 短期 | 请求时 Set-Cookie 续期 |
| `gid` | 匿名设备 ID | 长期 | - |

**关键观察**：`web_session` 是核心。只要它有效，其他 cookie 都可以通过正常 HTTP 请求的 Set-Cookie 获取。问题在于：**当 `web_session` 过期后，能否通过 API 而非浏览器重新获取？**

---

## 三、需要研究的方向

### 方向 A：`web_session` 的 Token Refresh 接口

**假设**：小红书可能在 cookie 或 localStorage 中存储了 refresh_token 类机制。

**研究方法**：
1. 在浏览器 DevTools → Application → Local Storage / Session Storage 中查找小红书域名下的存储项
2. 查看是否有 `refresh_token`、`token_expiry`、`session_key` 等字段
3. 监控 `/api/sns/web/v2/user/me` 响应，看是否返回 refresh 相关字段
4. 检查 IndexedDB 中是否有 token 存储

**如果存在**：可以构造 HTTP 请求直接用 refresh_token 换新 web_session，完全不需要浏览器。

### 方向 B：Web QR 登录 API 逆向

**原理**：Web 端扫码登录本质上是一组 HTTP API 调用，可以脱离浏览器直接调用。

**登录流程（需逆向验证）**：
```
1. POST /api/sns/web/v1/login/qrcode/create  → 获取 QR code URL + qr_id
2. 展示 QR code（可生成终端二维码或发送到手机）
3. POST /api/sns/web/v1/login/qrcode/status?qr_id=xxx  → 轮询扫码状态
4. 用户手机扫码确认后，该 API 返回 web_session cookie
```

**研究方法**：
1. 浏览器 DevTools → Network 过滤 `login/qrcode` 请求
2. 分析请求头（需要哪些签名 x-s, x-t 等）
3. 分析请求体和响应体结构
4. 验证是否需要签名（如果需要 x-s，则需要 JS 签名器，但已有的 `xhs_sign.py` 可以处理）

**价值**：如果能 API 化 QR 登录，就不再需要 Playwright 来做登录，只需要在终端显示二维码。

### 方向 C：手机号+验证码登录 API

**原理**：小红书 Web 端支持手机号登录，对应的 API 如果能逆向，可以实现全自动登录（配合短信转发服务）。

**研究方法**：
1. 浏览器 DevTools 监控手机号登录流程的 API 请求
2. 分析验证码发送/验证接口
3. 评估是否可以配合短信接码平台实现全自动

### 方向 D：App 端 API 登录

**原理**：小红书 App 端的登录 API 可能比 Web 端更容易逆向，且不依赖浏览器 cookie 体系。

**研究方法**：
1. 用抓包工具（Charles/mitmproxy）抓取 App 登录流程
2. 分析 App 端 API 的签名机制（通常比 Web 端简单）
3. 评估是否能用 App API 登录后获取的 token 转换为 Web cookie

---

## 四、优先级评估

| 方向 | 可行性 | 实现难度 | 自动化程度 | 优先级 |
|---|---|---|---|---|
| A. Token Refresh 接口 | 中（可能不存在） | 低 | ★★★★★ | **最高** |
| B. QR 登录 API 逆向 | 高 | 中 | ★★★★ | **高** |
| C. 手机号登录 API | 中 | 中 | ★★★（需短信） | 中 |
| D. App API 登录 | 中 | 高 | ★★★★ | 低（成本高） |

---

## 五、推荐的实施步骤

### Phase 1：调研（1-2 天）
1. **方向 A 验证**：在浏览器 DevTools 中全面搜索 localStorage、SessionStorage、IndexedDB，查看是否有 refresh token 机制
2. **方向 B 验证**：在浏览器中进行一次完整的 QR 扫码登录，用 DevTools 记录所有网络请求，重点分析 `/api/sns/web/v1/login/` 路径下的接口

### Phase 2：原型验证（2-3 天）
- 基于调研结果，选择最有希望的方案
- 用 Python `requests` + 现有的 `xhs_sign.py` 签名器实现一个最小原型
- 验证能否获取到有效的 `web_session`

### Phase 3：集成（1-2 天）
- 将验证成功的方案集成到 `xhs_login.py` 中作为一个新的 tier
- 在 `xhs_keepalive.py` 的 fallback 链中加入这个 tier
- 测试完整的多账号自动恢复流程

---

## 六、与现有代码的集成点

| 文件 | 修改 |
|---|---|
| `scripts/xhs_login.py` | 新增 `acquire_via_token_refresh()` 或 `acquire_via_qr_api()` |
| `scripts/xhs_login.py` | 在 `acquire_cookies()` 的 chain 中加入新 tier |
| `scripts/xhs_keepalive.py` | 在 fallback 链中加入新 tier（优先于 Profile 恢复） |
| `scripts/xhs_config.py` | 新增相关配置常量 |

---

## 七、风险提示

1. **平台反逆向**：小红书会定期更新 API 和加密方式，逆向成果可能失效
2. **法律合规**：自动化登录可能违反小红书用户协议，需评估法律风险
3. **账号风险**：频繁的自动化登录行为可能触发平台风控，导致账号被封
4. **维护成本**：逆向方案需要跟随平台更新持续维护

---

## 八、参考资料

- 项目现有的签名模块：`scripts/xhs_sign.py`（JS 签名执行 + Playwright 签名）
- 项目现有的登录模块：`scripts/xhs_login.py`（多档 fallback 登录）
- 小红书 Web API 基础 URL：`https://edith.xiaohongshu.com`
- 签名必需头：`x-s`、`x-t`、`x-s-common`（通过 `xhs_sign.py` 生成）

---

*报告生成时间：2026-05-21*
*基于项目版本：v1.6.0（commit 10a58b1）*
