# 稳定优先的浏览器×API 融合方案（高强度升级版）

> 更新时间：2026-05-28
> 适用场景：每天 7-8 小时高强度爬取 + 低强度场景
> 基于：XHS "阿瑞斯"风控体系逆向分析 + 行业最佳实践

---

## 零、XHS 阿瑞斯风控体系

XHS 的风控系统（代号"阿瑞斯"）在多层检测，**季度拦截 1596 亿次恶意请求**：

| 检测层 | 检测内容 | 对应我们的防线 |
|--------|---------|--------------|
| **签名验证** | x-s / x-t / x-s-common 正确性 | PlaywrightSigner（Layer 1） |
| **TLS 指纹** | JA3/JA4 是否像真实浏览器 | curl_cffi impersonate（已有） |
| **IP 频率 + 关联** | 同 IP 多账号在短窗口出现 | 代理轮换 + 限速（Layer 2） |
| **设备指纹** | Canvas / WebGL / AudioContext | Fingerprint 隔离（已有） |
| **行为轨迹** | 请求模式、导航流、浏览多样性 | **行为模拟（Layer 3，新增）** |
| **账号关联图** | ML 模型检测账号关联 | **账号隔离（Layer 4，新增）** |
| **10 分钟异常窗口** | 短时间行为异常 | **会话管理（Layer 3，新增）** |

**升级路径**：静默降级(空数据) → 验证码(461) → 限流(429) → 账号限制 → 永久封禁

### 高强度场景的安全边界

根据行业研究和 XHS 社区实测数据：

| 指标 | 安全值 | 警告值 | 危险值 |
|------|--------|--------|--------|
| 单账号每小时请求 | ≤40 | 40-60 | >60 |
| 单账号每小时搜索 | ≤20 | 20-30 | >30 |
| 单账号每日总请求 | ≤400 | 400-600 | >600 |
| 连续活跃时长 | ≤60min | 60-90min | >90min |
| 冷却间隔 | ≥60min | 30-60min | <30min |
| 浏览多样性（非爬取请求占比） | ≥15% | 5-15% | <5% |

**7-8 小时 × 40请求/小时 = 320 请求/天/账号**。如果需要更多，必须多账号。

---

## 一、根因分析（保留）

### 用户遇到的问题

**现象**：低强度爬取（每天2小时），连续正常几天后，某天突然出现 -104。

### -104 的可能原因排查

| 可能原因 | 是否吻合 | 判断依据 |
|----------|---------|---------|
| 频率限制（太快） | **不吻合** | 每天2小时不算高强度，且前几天正常 |
| IP 信誉差 | **不吻合** | 同 IP 前几天正常 |
| 行为检测（机器人） | **不吻合** | 低频+有间歇，不像自动化行为 |
| **b1/fff 令牌过期** | **吻合** | 前几天 xhs_main.js 中的 fff 仍有效，XHS 服务端更新 b1 后旧 fff 失效 |

### b1 = fff 的关系

```
xhs_main.js:96   localStorage["b1"] = "I38rHdgs..."   ← 浏览器中动态更新
xhs_main.js:400  var fff = "I38rHdgs..."               ← JS文件中硬编码（同值快照）
xhs_main.js:415  x8: fff                               ← 直接嵌入 x-s-common
xhs_main.js:403  MD5(xt + xs + fff)                    ← 参与 MD5 计算 → x9
```

PlaywrightSigner 用真实浏览器，b1 始终从 localStorage 实时读取 → **b1 过期被彻底消除**。

---

## 二、五层防御架构

```
┌─ Layer 1: 签名稳定 ─────────────────────────────────────┐
│  PlaywrightSigner + b1 收割 + EmbedJsSigner 后备         │
│  解决: b1 过期 / 签名算法轮换 / Cookie 续期               │
└──────────────────────────────────────────────────────────┘
┌─ Layer 2: 传输稳定 ─────────────────────────────────────┐
│  403 换代理 / 429 降速 / 461 headless 接管 / IP 限速      │
│  解决: IP 封禁 / 频率限制 / 验证码                        │
└──────────────────────────────────────────────────────────┘
┌─ Layer 3: 会话稳定 (新增) ──────────────────────────────┐
│  浏览器定时刷新 / 会话间歇休息 / 请求多样性               │
│  解决: 长时间连续活跃检测 / 会话老化 / 行为模式单一        │
└──────────────────────────────────────────────────────────┘
┌─ Layer 4: 账号稳定 (新增) ──────────────────────────────┐
│  多账号时间窗口轮换 / 账号隔离 / 日请求安全上限            │
│  解决: 单账号过热 / 账号关联检测                           │
└──────────────────────────────────────────────────────────┘
┌─ Layer 5: 可观测性 ─────────────────────────────────────┐
│  请求日志 + 风控事件统计 + 健康度仪表盘                    │
│  解决: 不知道什么时候该停 / 不知道哪个环节出问题            │
└──────────────────────────────────────────────────────────┘
```

---

## 三、Layer 1-2：签名 + 传输（与之前方案相同）

### 3.1 Signing Oracle + Token Cache

```
PlaywrightSigner (常驻 headless 浏览器)
  ├─ 签名: page.evaluate("window._webmsxyw(url, data)")
  ├─ b1 收割: 每 100 次签名 → extract → b1_cache.json
  ├─ 浏览器心跳: 每次 _ensure_browser() 检测 page.evaluate("1+1")
  └─ 崩溃恢复: 3 次失败 → 降级 EmbedJsSigner → 每 50 次尝试恢复

EmbedJsSigner (后备)
  ├─ 加载 b1_cache.json → inject_b1() → 替换 xhs_main.js 中的 fff
  └─ JS 签名 ~50ms (无浏览器依赖)

Transport Layer (Fetcher)
  ├─ curl_cffi + TLS 指纹匹配
  ├─ 代理轮换 / 账号轮换 / burst+rest 节流
  └─ -104 紧急 b1 刷新 / 403 换代理 / 429 降速 / 461 headless
```

（详细实现代码见下方第四节改动 1-6）

---

## 四、Layer 3：会话稳定（新增）

### 4.1 浏览器定时刷新

**问题**：PlaywrightSigner 常驻 7-8 小时，浏览器内存泄漏、缓存膨胀、session 特征老化。

**解决**：每 2-3 小时主动关闭并重启浏览器。

```
PlaywrightSigner:
  self._browser_start_time: float = 0
  self._browser_max_age: float = 7200  # 2小时

  def _ensure_browser(self):
      if self._page is not None:
          # 心跳检测
          try:
              self._page.evaluate("1+1")
              # 检查是否到了刷新时间
              if time.time() - self._browser_start_time > self._browser_max_age:
                  print("[SIGN] 浏览器会话已超过 2 小时，主动刷新...", file=sys.stderr)
                  self.close()
                  # 不 return，继续往下重新启动
              else:
                  return
          except Exception:
              print("[SIGN] 浏览器进程已死，重启...", file=sys.stderr)
              self.close()
      # 启动新浏览器
      ...原有启动逻辑...
      self._browser_start_time = time.time()
```

### 4.2 会话间歇休息

**问题**：真人不可能连续浏览 60 分钟不停。阿瑞斯检测 10 分钟异常窗口。

**解决**：Fetcher 每 45-60 分钟主动休息 10-20 分钟。

```
Fetcher:
  self._session_start: float = time.time()
  self._session_active_duration: float = random.uniform(2700, 3600)  # 45-60min
  self._session_rest_duration: tuple = (600, 1200)  # 10-20min 休息

  def _maybe_session_rest(self):
      if time.time() - self._session_start < self._session_active_duration:
          return
      rest = random.uniform(*self._session_rest_duration)
      print(f"[FETCH] 会话休息 {rest/60:.1f} 分钟（模拟人类离开）...", file=sys.stderr)
      time.sleep(rest)
      self._session_start = time.time()
      self._session_active_duration = random.uniform(2700, 3600)  # 下次随机

  # 在 _call() 中调用：
  def _call(self, method, api, params, data):
      self._maybe_session_rest()  # 新增
      ...
```

### 4.3 请求模式多样性

**问题**：只访问搜索/详情/评论三个 API，模式太单一，被阿瑞斯立即标记。

**解决**：每 N 次爬取请求，穿插一次辅助请求。

```
Fetcher:
  self._auxiliary_interval: int = 10  # 每 10 次真实请求穿插 1 次辅助请求

  def _maybe_auxiliary_request(self):
      """穿插辅助请求，增加浏览多样性。"""
      if self.request_count % self._auxiliary_interval != 0:
          return
      actions = [
          # 访问推荐流（模拟浏览首页）
          ("POST", "/api/sns/web/v1/homefeed", {
              "cursor_score": "", "num": 18, "refresh_type": 1,
              "category": "homefeed_recommend",
          }),
          # 访问用户信息（模拟查看自己主页）
          ("GET", "/api/sns/web/v2/user/me", None),
      ]
      method, api, data = random.choice(actions)
      try:
          print(f"[FETCH] 辅助请求: {api}（增加浏览多样性）", file=sys.stderr)
          self._call_raw(method, api, None, data, count=False)
          time.sleep(random.uniform(3, 8))  # 模拟阅读
      except Exception:
          pass  # 辅助请求失败不影响主流程
```

### 4.4 搜索间歇特别处理

搜索 API 风控最严格。连续搜索之间需要更长的间隔和更多的穿插。

```
# 在 search 命令的处理中：
# 每搜索 5 个关键词 → 强制休息 15-30 分钟 + 穿插 2-3 次辅助请求
# 搜索间隔使用 "search" speed profile（30-60s 间隔）
```

---

## 五、Layer 4：账号稳定（新增）

### 5.1 多账号时间窗口轮换

**当前**：LRU 轮换 + 风控冷却。问题是没有主动的轮换节奏，可能一个账号连续跑太久。

**增强**：基于时间窗口的主动轮换。

```
账号生命周期:
  WARMING_UP (5min, 2-3个辅助请求) → ACTIVE (30-60min) → COOLING_DOWN (60-120min) → READY

触发轮换条件（最早触发者胜出）:
  - 时间: 活跃超过 45 分钟
  - 请求数: 本窗口超过 40 次
  - 风控: 任何 460/461

实现: 在 Fetcher._rotate_account() 中增加基于时间的主动轮换
```

### 5.2 日请求安全上限

**当前**：`DAILY_HARD_CAP = 500`。对于 7-8 小时场景，这个值刚好在安全边界。

**建议**：
- 5 个账号 × 400 请求 = 2000 请求/天（满足 7-8 小时需求）
- 每个账号的 DAILY_HARD_CAP 保持 500（有余量，但实际目标 400）
- 在配置文件中可调：`daily_hard_cap = 400`

### 5.3 账号隔离

**原则**：一个账号 = 一个稳定的指纹 + 一个一致的 IP。

```
当前已有:
  ✓ 每账号独立 fingerprint (UA/sec-ch-ua/impersonate)
  ✓ 每账号独立 cookies
  ✓ 每账号独立 cooldown 状态
  ✓ 每账号可绑定 proxy_url

需要增强:
  - 账号启动时间错开 15-30 分钟（避免同时激活）
  - 同时活跃的账号不超过 2-3 个
```

---

## 六、实现改动清单

### Layer 1-2（签名 + 传输）— 与之前方案相同

| # | 改动 | 文件 | 说明 |
|---|------|------|------|
| 1 | 翻转 DEFAULT_CHAIN | `xhs_sign.py` | `("playwright", "embed-js", "py-port")` |
| 2 | harvest_b1 | `xhs_sign.py` | PlaywrightSigner 每 100 次收割 b1 |
| 3 | inject_b1 | `xhs_sign.py` | EmbedJsSigner 加载/注入 b1 |
| 4 | b1 回调 | `xhs_sign.py` | AutoSigner 同步 b1 到 EmbedJsSigner |
| 5 | 心跳 + 定时刷新 | `xhs_sign.py` | _ensure_browser 心跳 + 2h 定时重启 |
| 6 | 风控修复 | `xhs_fetcher.py` | -104/403/429/461 增强 |
| 7 | extract_b1_standalone | `xhs_sign.py` | 临时浏览器提取 b1 |
| 8 | Keepalive b1 | `xhs_keepalive.py` | Tier 1 b1 收割 |

### Layer 3（会话稳定）— 新增

| # | 改动 | 文件 | 说明 |
|---|------|------|------|
| 9 | 浏览器定时刷新 | `xhs_sign.py` | _ensure_browser 中检查 _browser_max_age |
| 10 | 会话间歇休息 | `xhs_fetcher.py` | _maybe_session_rest() 每 45-60min 休息 10-20min |
| 11 | 请求多样性 | `xhs_fetcher.py` | _maybe_auxiliary_request() 每 10 次穿插辅助请求 |

### Layer 4（账号稳定）— 新增

| # | 改动 | 文件 | 说明 |
|---|------|------|------|
| 12 | 时间窗口轮换 | `xhs_fetcher.py` | _rotate_account() 增加基于时间的主动轮换 |
| 13 | 活跃账号数限制 | `xhs_accounts.py` | next_available() 跳过活跃时间内的账号 |

---

## 七、详细实现代码

### 改动 1-8（Layer 1-2，与之前相同，此处省略）
详见 HYBRID_STRATEGY.md 第四节，代码完全一致。

### 改动 9: 浏览器定时刷新

**文件**: `scripts/xhs_sign.py` — PlaywrightSigner._ensure_browser()

```python
def __init__(self, ...):
    ...
    self._browser_start_time: float = 0.0
    self._browser_max_age: float = 7200  # 2 小时刷新一次

def _ensure_browser(self) -> None:
    if self._page is not None:
        try:
            self._page.evaluate("1+1")
            age = time.time() - self._browser_start_time
            if age > self._browser_max_age:
                print(f"[SIGN] 浏览器会话已运行 {age/3600:.1f}h，主动刷新", file=sys.stderr)
                self.close()
            else:
                return
        except Exception:
            print("[SIGN] 浏览器心跳失败，重启...", file=sys.stderr)
            self.close()
    # ... 原有启动逻辑 ...
    self._browser_start_time = time.time()
```

### 改动 10: 会话间歇休息

**文件**: `scripts/xhs_fetcher.py` — Fetcher

```python
def __init__(self, ...):
    ...
    self._session_start: float = time.time()
    self._session_active_duration: float = random.uniform(2700, 3600)  # 45-60min

def _maybe_session_rest(self) -> None:
    """每 45-60 分钟主动休息 10-20 分钟，模拟人类离开。"""
    if time.time() - self._session_start < self._session_active_duration:
        return
    rest = random.uniform(600, 1200)  # 10-20 分钟
    print(f"[FETCH] 会话休息 {rest/60:.1f} 分钟（模拟离开）...", file=sys.stderr)
    time.sleep(rest)
    self._session_start = time.time()
    self._session_active_duration = random.uniform(2700, 3600)
    # 休息后重置 warmed，重新做一次 warmup
    self._warmed = False
```

### 改动 11: 请求多样性

**文件**: `scripts/xhs_fetcher.py` — Fetcher

```python
def __init__(self, ...):
    ...
    self._auxiliary_counter: int = 0

def _maybe_auxiliary_request(self) -> None:
    """每 10 次真实请求穿插 1 次辅助请求，增加浏览多样性。"""
    self._auxiliary_counter += 1
    if self._auxiliary_counter % 10 != 0:
        return
    actions = [
        ("POST", "/api/sns/web/v1/homefeed", {
            "cursor_score": "", "num": 18, "refresh_type": 1,
            "note_index": 0, "category": "homefeed_recommend",
        }),
        ("GET", "/api/sns/web/v2/user/me", None),
    ]
    method, api, data = random.choice(actions)
    try:
        print(f"[FETCH] 辅助请求: {api}（增加多样性）", file=sys.stderr)
        self._call_raw(method, api, None, data, count=False)
        time.sleep(random.uniform(3, 8))
    except Exception:
        pass
```

### 改动 12: 时间窗口主动轮换

**文件**: `scripts/xhs_fetcher.py` — Fetcher._call() / _rotate_account()

```python
def __init__(self, ...):
    ...
    self._account_active_start: float = time.time()
    self._account_max_active: float = random.uniform(2400, 3600)  # 40-60min

def _should_rotate_for_freshness(self) -> bool:
    """基于时间主动轮换账号，不等风控触发。"""
    active_time = time.time() - self._account_active_start
    if active_time > self._account_max_active:
        print(f"[FETCH] 账号 {self.account.alias} 已活跃 {active_time/60:.0f}min，主动轮换",
              file=sys.stderr)
        return True
    if self.request_count > 0 and self.request_count % 40 == 0:
        print(f"[FETCH] 账号 {self.account.alias} 已请求 {self.request_count} 次，主动轮换",
              file=sys.stderr)
        return True
    return False

def _call(self, method, api, params, data):
    ...
    self._maybe_session_rest()
    # 新增：主动轮换检查
    if self._should_rotate_for_freshness():
        self._rotate_account("主动轮换")
        self._account_active_start = time.time()
        self._account_max_active = random.uniform(2400, 3600)
    self.warmup()
    self._throttle()
    result = self._call_raw(method, api, params, data, count=True)
    self._maybe_auxiliary_request()  # 新增
    self._maybe_refresh_cookies()
    return result
```

---

## 八、7-8 小时运行时间线示例

```
00:00  启动 → PlaywrightSigner 懒启动 → warmup(首页推荐)
00:01  账号A 开始爬取（search + note）
00:10  第 10 次请求 → 穿插辅助请求(homefeed)
00:30  第 20 次请求 → long_rest 30min（paranoid 模式）
00:35  第 30 次请求 → long_rest 30min
00:45  账号A 活跃 45min → 主动轮换到账号B
       账号B warmup → 开始爬取
01:00  第 10 次请求(B) → 穿插辅助请求(user/me)
01:05  会话休息 10-20min（整体休息）
01:20  恢复 → 账号B 继续爬取
01:30  账号B 活跃 45min → 轮换到账号C
       ...（如此循环）
02:00  PlaywrightSigner 浏览器满 2h → 主动刷新（关闭+重启）
02:05  浏览器重启完成 → harvest_b1 → 继续
       ...
04:00  第二次浏览器刷新
06:00  第三次浏览器刷新
07:00  最后一个账号完成 → 爬取结束
```

每个账号实际活跃时间：~45-60min/轮，每轮 30-40 次请求。
5 个账号循环：每账号每天约 320-400 请求。总请求 ~1600-2000。

---

## 九、修改文件清单（完整）

| 文件 | 改动规模 | Layer |
|------|---------|-------|
| `scripts/xhs_sign.py` | ~90 行 | L1 + L3 |
| `scripts/xhs_fetcher.py` | ~70 行 | L2 + L3 + L4 |
| `scripts/xhs_keepalive.py` | ~15 行 | L1 |

总计 ~175 行改动，不新增文件。

---

## 十、验证计划

1. **短时间验证**（30 分钟）:
   ```bash
   python -m xhs search test --limit 20
   # 观察日志: PlaywrightSigner 启动、warmup、辅助请求
   ```

2. **会话休息验证**:
   ```bash
   python -m xhs search test --limit 100
   # 观察日志: 45-60min 后出现 "会话休息"
   # 观察日志: 每 10 次出现 "辅助请求"
   ```

3. **多账号轮换验证**:
   ```bash
   python -m xhs crawl keyword --accounts 5
   # 观察日志: 账号轮换、每个账号不超过 40 请求/45min
   ```

4. **长时间压力测试**:
   ```bash
   python -m xhs serve
   # 运行 7-8 小时，观察:
   # - 无 FatalRiskError
   # - 浏览器每 2h 刷新一次
   # - 账号轮换均匀
   # - 会话休息正常触发
   ```

5. **已有 133 个测试**: 全部通过

---

## 十一、效果预期

| 问题 | 之前 | Layer 1-2 | 完整五层 |
|------|------|-----------|---------|
| b1 过期 → -104 | 必然发生 | **消除** | **消除** |
| 签名算法轮换 | 整个签名层失效 | **消除** | **消除** |
| 连续活跃检测 | 无防护 | 无防护 | **45-60min 主动休息** |
| 行为模式单一 | 无防护 | 无防护 | **辅助请求增加多样性** |
| 浏览器会话老化 | 无防护 | 无防护 | **2h 定时刷新** |
| 单账号过热 | 风控后才冷却 | 无变化 | **40请求/45min 主动轮换** |
| 403 IP 封禁 | 等 300s | 立即换代理 | 立即换代理 |
| 429 限流 | 不降速 | 降速 | 降速 |
| 461 验证码(自主) | 崩溃 | headless 接管 | headless 接管 |

**预期**: 7-8 小时运行，5 个账号轮换，整体 -104/460/461 发生率降低 90%+。
