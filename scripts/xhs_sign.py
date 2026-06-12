"""三档签名 + auto 路由 + 失败窗口降级。

签名层产出小红书 web API 必需的请求头：x-s / x-t / x-s-common / x-b3-traceid /
x-xray-traceid / x-rap-param。

三档：
  1. EmbedJsSigner    — py_mini_racer/execjs 跑 assets/xhs_main.js。快，且生成完整签名头，默认首选。
  2. PlaywrightSigner — 真实浏览器跑 window._webmsxyw。免维护算法，最稳，但缺少 x-xray-traceid。
  3. PyPortSigner     — 纯 Python 端口。MVP 阶段仅占位，未实现。

CLI 通过 --sign-mode 选择；默认 auto 按 fallback_chain 探针。
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

XHS_MAIN_JS = ASSETS / "xhs_main.js"
XHS_RAP_JS = ASSETS / "xhs_rap.js"
XHS_XRAY_JS = ASSETS / "xhs_xray.js"
CRYPTO_JS = ASSETS / "crypto-js.min.js"
JS_VERSION_PATH = ROOT / "data" / "js_version.json"
B1_CACHE_PATH = ROOT / "data" / "b1_cache.json"

# 给 bare V8（py_mini_racer）一个最小 Node 兼容层：window / global / globalThis / require("crypto-js")
# crypto-js.min.js 是 UMD，会把 CryptoJS 挂到 this 上；xhs_main.js 的"补环境"代码也依赖 global。
_NODE_SHIM = """
var global = (function() { return this; })();
var globalThis = global;
var window = global;
var self = global;
var __modules = {};
function require(name) {
  if (name === 'crypto-js' || name === 'CryptoJS') {
    return global.CryptoJS || __modules['crypto-js'];
  }
  throw new Error('module not bundled: ' + name);
}
var module = { exports: {} };
var exports = module.exports;
"""


def random_b3_traceid(n: int = 16) -> str:
    return "".join(random.choice("abcdef0123456789") for _ in range(n))


def _load_js_version() -> dict:
    """读取签名 JS 版本信息。优先从 js_version.json，fallback 到 JS 文件头部注释。"""
    if JS_VERSION_PATH.exists():
        try:
            return json.loads(JS_VERSION_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # fallback: 从 JS 文件头部注释提取 commit
    import re
    try:
        head = XHS_MAIN_JS.read_text(encoding="utf-8", errors="ignore")[:200]
        m = re.search(r"commit:\s*(\S+)", head)
        if m:
            return {"commit_short": m.group(1).rstrip(",")}
        # 没有注释时，用文件修改时间
        from datetime import datetime, timezone
        mtime = XHS_MAIN_JS.stat().st_mtime
        return {"commit_short": f"file-{datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y%m%d')}",
                "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()}
    except Exception:
        return {}


def _js_version_suffix() -> str:
    """构建版本后缀字符串，用于日志和警告。"""
    ver = _load_js_version()
    short = ver.get("commit_short", "")
    if short:
        updated = ver.get("updated_at", "")[:10]
        return f" (JS 版本: {short}, 更新于 {updated})"
    return " (JS 版本: 未知, 请运行 update-js)"


def check_js_staleness(warn_days: int = 30, critical_days: int = 60) -> str | None:
    """检查签名 JS 文件是否过期。返回警告消息或 None。"""
    if not XHS_MAIN_JS.exists():
        return None
    age_days = (time.time() - XHS_MAIN_JS.stat().st_mtime) / 86400
    ver_suffix = _js_version_suffix()
    if age_days > critical_days:
        return (f"[WARN] 签名 JS 已 {int(age_days)} 天未更新（>{critical_days}天），"
                f"签名大概率失效！建议: python scripts/xhs.py update-js{ver_suffix}")
    if age_days > warn_days:
        return (f"[WARN] 签名 JS 已 {int(age_days)} 天未更新（>{warn_days}天），"
                f"建议: python scripts/xhs.py update-js{ver_suffix}")
    return None


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class SignError(RuntimeError):
    pass


class SignerBase(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self._recent: deque[bool] = deque(maxlen=20)

    @abstractmethod
    def sign(self, api: str, data: Any, a1: str, method: str = "POST",
             platform: str = "") -> dict[str, str]:
        """返回 dict: x-s / x-t / x-s-common / x-b3-traceid / x-xray-traceid / x-rap-param"""

    def record(self, ok: bool) -> None:
        self._recent.append(ok)

    def recent_failures(self) -> int:
        # 最近 3 次连续失败 → 触发降级
        if len(self._recent) < 3:
            return 0
        return sum(1 for v in list(self._recent)[-3:] if not v)

    def health(self) -> str:
        if not self._recent:
            return "unknown"
        rate = sum(self._recent) / len(self._recent)
        return f"{rate*100:.0f}% ({sum(self._recent)}/{len(self._recent)})"


# ---------------------------------------------------------------------------
# JS engine wrapper (py_mini_racer 优先 / execjs fallback)
# ---------------------------------------------------------------------------

class _JsCtx:
    """简包装：暴露 .call(funcname, *args)。"""

    def __init__(self, source: str, engine: str, cwd: Path | None = None):
        self.engine = engine
        if engine == "mini-racer":
            from py_mini_racer import MiniRacer  # type: ignore
            self._ctx = MiniRacer()
            self._ctx.eval(source)
        elif engine == "execjs":
            import execjs  # type: ignore
            # 将 cwd 绝对路径注入 source，让 require() 用绝对路径解析 node_modules
            if cwd:
                import os
                cwd_abs = str(cwd).replace("\\", "/")
                source = f"process.chdir('{cwd_abs}');\n{source}"
            self._ctx = execjs.compile(source)
        else:
            raise SignError(f"unknown js engine {engine}")

    def call(self, fn: str, *args: Any) -> Any:
        if self.engine == "mini-racer":
            json_args = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
            return self._ctx.eval(f"{fn}({json_args})")
        return self._ctx.call(fn, *args)


def _pick_engine() -> str:
    # 小红书签名 JS 依赖 Node + crypto-js + 较完整的浏览器环境，
    # 因此优先 execjs（Node）；mini-racer 仅在 Node 缺失时作为最后的尝试。
    try:
        import execjs  # noqa: F401
        return "execjs"
    except ImportError:
        try:
            import py_mini_racer  # noqa: F401
            return "mini-racer"
        except ImportError as e:
            import platform as _pf
            _os = _pf.system()
            if _os == "Darwin":
                node_hint = "brew install node"
            elif _os == "Windows":
                node_hint = "winget install OpenJS.NodeJS（或从 nodejs.org 下载）"
            else:
                node_hint = "sudo apt install nodejs（或 yum/dnf install nodejs）"
            raise SignError(
                f"需要 PyExecJS（推荐，需 Node.js）或 py_mini_racer。\n"
                f"安装 Node.js: {node_hint}\n"
                f"然后: pip install PyExecJS && cd assets && npm install crypto-js"
            ) from e


# ---------------------------------------------------------------------------
# EmbedJsSigner
# ---------------------------------------------------------------------------

class EmbedJsSigner(SignerBase):
    name = "embed-js"

    def __init__(self) -> None:
        super().__init__()
        if not XHS_MAIN_JS.exists():
            raise SignError(f"missing {XHS_MAIN_JS}; 见 README '签名 JS 热更新'")
        _msg = check_js_staleness()
        if _msg:
            print(_msg, file=sys.stderr)
        ver = _load_js_version()
        print(f"[SIGN] JS 版本: {ver.get('commit_short', '未知')}", file=sys.stderr)
        self._engine = _pick_engine()
        self._ctx_main: _JsCtx | None = None
        self._ctx_rap: _JsCtx | None = None
        self._ctx_xray: _JsCtx | None = None
        self._mtime_main: float = 0.0
        self._mtime_rap: float = 0.0
        self._mtime_xray: float = 0.0
        self._cached_b1: str = ""
        self._last_platform: str = ""  # 上次构建时的平台，用于按账号重建
        self._try_load_b1_cache()
        self._reload_if_stale()

    def _reload_if_stale(self, platform: str = "") -> None:
        # b1 注入后需强制重建（_mtime_main 已被置 0）
        m_main = XHS_MAIN_JS.stat().st_mtime
        platform_changed = platform and platform != self._last_platform
        if m_main != self._mtime_main or platform_changed:
            self._ctx_main = self._build_ctx(XHS_MAIN_JS, platform=platform)
            self._mtime_main = m_main
            self._last_platform = platform
            print(f"[SIGN] loaded {XHS_MAIN_JS.name} via {self._engine}"
                  f"{' (platform=' + platform + ')' if platform else ''}",
                  file=sys.stderr)
        if XHS_RAP_JS.exists():
            m_rap = XHS_RAP_JS.stat().st_mtime
            if m_rap != self._mtime_rap:
                self._ctx_rap = self._build_ctx(XHS_RAP_JS)
                self._mtime_rap = m_rap
        if XHS_XRAY_JS.exists():
            m_xray = XHS_XRAY_JS.stat().st_mtime
            if m_xray != self._mtime_xray:
                self._ctx_xray = self._build_ctx(XHS_XRAY_JS)
                self._mtime_xray = m_xray

    def _build_ctx(self, js_path: Path, platform: str = "") -> "_JsCtx":
        """组装 JS 签名上下文，注入 b1 和平台相关信息。"""
        import re as _re
        source = js_path.read_text(encoding="utf-8")
        # 注入 b1 令牌（替换 xhs_main.js 中硬编码的 fff）
        if self._cached_b1:
            source = _re.sub(r'var\s+fff\s*=\s*"[^"]*"',
                             f'var fff = "{self._cached_b1}"',
                             source, count=1)
        # 注入平台信息：x2 字段、Navigator UA OS 片段、Edge 后缀
        if platform and "x2:" in source:
            # 1) x2: "Windows" → x2: "{platform}"（两处：seccore_signv2 + XsCommon）
            source = _re.sub(r'x2:\s*"Windows"', f'x2: "{platform}"', source)
            # 2) Navigator UA OS 片段
            if platform == "macOS":
                source = _re.sub(
                    r'\(Windows NT \d+\.\d+; Win64; x64\)',
                    '(Macintosh; Intel Mac OS X 10_15_7)', source)
            elif platform == "Windows":
                # 确保是 Windows 格式（防止上次是 Mac 后残留）
                source = _re.sub(
                    r'\(Macintosh; Intel Mac OS X \d+_\d+_\d+\)',
                    '(Windows NT 10.0; Win64; x64)', source)
            # 3) 非 Edge 指纹移除 Edg 后缀（Edge 浏览器标识不应出现在 Chrome 指纹中）
            #    如果 UA 中有 Edg/ 但指纹不是 Edge 类型，移除它
            #    这里简单处理：如果 platform 为空（默认不区分 Edge），保持原样
            #    Edge 移除逻辑由 fetcher 根据 fingerprint.is_edge 控制
        if self._engine == "execjs":
            # 把 require("crypto-js") 改成项目内绝对路径，避免依赖子进程 cwd
            crypto_node = ASSETS / "node_modules" / "crypto-js"
            if crypto_node.exists():
                abs_path = str(crypto_node).replace("\\", "/")
                source = source.replace('require("crypto-js")', f'require("{abs_path}")')
                source = source.replace("require('crypto-js')", f"require('{abs_path}')")
            # xhs_xray.js 的 require('./xhs_xray_packN.js') 是相对于临时脚本文件，
            # execjs 把 source 写到临时目录执行，相对路径无法解析 → 重写为绝对路径
            assets_abs = str(ASSETS).replace("\\", "/")
            for pack in ("xhs_xray_pack1.js", "xhs_xray_pack2.js"):
                pack_abs = f"{assets_abs}/{pack}"
                source = source.replace(f"require('./{pack}')", f"require('{pack_abs}')")
                source = source.replace(f'require("./{pack}")', f'require("{pack_abs}")')
                source = source.replace(f"require('../static/{pack}')", f"require('{pack_abs}')")
                source = source.replace(f"require('./static/{pack}')", f"require('{pack_abs}')")
            return _JsCtx(source, self._engine, cwd=ASSETS)
        # mini-racer：勉强尝试，多数情况会失败
        parts: list[str] = [_NODE_SHIM]
        if CRYPTO_JS.exists():
            parts.append(CRYPTO_JS.read_text(encoding="utf-8"))
        parts.append(source)
        return _JsCtx("\n;\n".join(parts), self._engine)

    def _try_load_b1_cache(self) -> None:
        """从 b1_cache.json 加载缓存的 b1。超过 24 小时的缓存自动失效。"""
        if not B1_CACHE_PATH.exists():
            return
        try:
            cache = json.loads(B1_CACHE_PATH.read_text(encoding="utf-8"))
            b1 = cache.get("b1", "")
            updated_at = cache.get("updated_at", 0)
            if b1:
                age = time.time() - updated_at
                if age > 86400:  # 24 小时过期
                    print(f"[SIGN] 缓存 b1 已过期（{int(age / 3600)}h 前），跳过",
                          file=sys.stderr)
                    return
                self._cached_b1 = b1
                print(f"[SIGN] 加载缓存 b1: {b1[:16]}...", file=sys.stderr)
        except Exception:
            pass

    def inject_b1(self, b1: str | None = None, force: bool = False) -> bool:
        """注入 b1 到 JS 签名上下文。返回是否注入了新值。"""
        if b1 is None:
            self._try_load_b1_cache()
            b1 = self._cached_b1
        if not b1:
            return False
        if b1 == self._cached_b1 and not force:
            return False
        print(f"[SIGN] 注入新 b1: {b1[:16]}...", file=sys.stderr)
        self._cached_b1 = b1
        self._mtime_main = 0  # 强制重建上下文
        self._reload_if_stale()
        return True

    def sign(self, api: str, data: Any, a1: str, method: str = "POST",
             platform: str = "") -> dict[str, str]:
        self._reload_if_stale(platform=platform)
        if self._ctx_main is None:
            raise SignError("EmbedJsSigner 上下文未初始化")
        data_str = "" if not data else (data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        try:
            ret = self._ctx_main.call("get_request_headers_params", api, data_str, a1, method)
            xs = ret["xs"]
            xt = str(ret["xt"])
            xs_common = ret["xs_common"]
        except Exception as e:
            raise SignError(f"xhs_main.js 调用失败：{e}") from e

        headers = {
            "x-s": xs,
            "x-t": xt,
            "x-s-common": xs_common,
            "x-b3-traceid": random_b3_traceid(),
        }
        if self._ctx_xray:
            try:
                headers["x-xray-traceid"] = self._ctx_xray.call("traceId")
            except Exception as e:
                print(f"[SIGN] x-xray-traceid 生成失败（缺 pack 文件?）: {str(e)[:80]}",
                      file=sys.stderr)
        if self._ctx_rap:
            try:
                headers["x-rap-param"] = self._ctx_rap.call(
                    "generate_x_rap_param", api, data_str, None
                )
            except Exception:
                pass  # x-rap-param 非必需，XHS API 不需要此头也能正常返回
        return headers


# ---------------------------------------------------------------------------
# PlaywrightSigner
# ---------------------------------------------------------------------------

class PlaywrightSigner(SignerBase):
    """搭桥：起常驻 headless Chromium，导航到 xiaohongshu.com，调用 window._webmsxyw。

    第一次 sign() 时懒启动浏览器；close() 关闭。
    """

    name = "playwright"

    def __init__(self, headless: bool = True, persist_dir: Path | None = None) -> None:
        super().__init__()
        self.headless = headless
        self.persist_dir = persist_dir or (ROOT / "data" / "pw_profile")
        self._pw = None
        self._browser = None
        self._page = None
        self._browser_start_time: float = 0.0
        self._browser_max_age: float = 7200  # 2 小时刷新一次
        self._sign_count: int = 0

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
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as e:
            raise SignError("需要 playwright。pip install playwright && playwright install chromium") from e
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.persist_dir),
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._page = self._browser.new_page()
            # 注入 stealth 脚本隐藏 Playwright 自动化特征
            self._page.add_init_script("""
// 隐藏 navigator.webdriver（Playwright/自动化标记）
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// 补全 window.chrome（headless Chromium 缺失该对象）
if (!window.chrome) window.chrome = {runtime: {}, csi: function(){}, loadTimes: function(){}};
// 补全权限查询（避免 notifications 权限查询暴露自动化）
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({state: Notification.permission}) :
    _origQuery(parameters)
);
""")
            try:
                from playwright_stealth import stealth_sync  # type: ignore
                stealth_sync(self._page)
            except ImportError:
                pass
            self._page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
            # 等 _webmsxyw 注入完成
            self._page.wait_for_function("typeof window._webmsxyw === 'function'", timeout=15000)
            self._browser_start_time = time.time()
        except Exception as e:
            print(f"[SIGN] 浏览器初始化失败: {e}，清理资源...", file=sys.stderr)
            self.close()
            raise SignError(f"PlaywrightSigner 浏览器初始化失败: {e}") from e

    def sign(self, api: str, data: Any, a1: str, method: str = "POST",
             platform: str = "") -> dict[str, str]:
        self._ensure_browser()
        if self._page is None:
            raise SignError("PlaywrightSigner 页面未初始化")
        data_str = "" if not data else (data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        ts = int(time.time() * 1000)
        try:
            # window._webmsxyw 接受 (url, data) → 返回 { X-s, X-t }
            ret = self._page.evaluate(
                "([url, body]) => window._webmsxyw(url, body)",
                [api, data_str or {}],
            )
        except Exception as e:
            raise SignError(f"playwright sign failed: {e}") from e
        # 每 20 次签名收割 b1
        self._sign_count += 1
        if self._sign_count % 20 == 0:
            self._harvest_b1()
        # 生成 x-xray-traceid：优先用浏览器 crypto API，失败则降级到 Python random
        xray_traceid = ""
        try:
            xray_traceid = self._page.evaluate(
                "() => { try { return crypto.randomUUID().replace(/-/g, ''); } catch(e) { return ''; } }"
            )
        except Exception:
            pass
        if not xray_traceid:
            xray_traceid = random_b3_traceid(32)
        return {
            "x-s": ret.get("X-s") or ret.get("x-s") or "",
            "x-t": str(ret.get("X-t") or ret.get("x-t") or ts),
            "x-s-common": ret.get("x-s-common", ""),
            "x-b3-traceid": random_b3_traceid(),
            "x-xray-traceid": xray_traceid,
        }

    def _harvest_b1(self) -> None:
        """从浏览器 localStorage 收割 b1 令牌并缓存（原子写入）。"""
        try:
            b1 = self._page.evaluate('localStorage.getItem("b1")') or ""
            if b1:
                B1_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                data = json.dumps({"b1": b1, "updated_at": time.time()}, ensure_ascii=False)
                tmp = B1_CACHE_PATH.with_suffix('.tmp')
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(B1_CACHE_PATH)
                print(f"[SIGN] 收割 b1: {b1[:16]}...（每 20 次签名）", file=sys.stderr)
        except Exception as e:
            print(f"[SIGN] b1 收割失败: {e}", file=sys.stderr)

    def get_b1(self) -> str:
        """获取浏览器最新的 b1 值。"""
        try:
            if self._page:
                return self._page.evaluate('localStorage.getItem("b1")') or ""
        except Exception:
            pass
        return ""

    def fetch_api(self, method: str, api: str, params: dict | None = None,
                  data: dict | None = None) -> dict:
        """用真实浏览器发起 API 请求（绕过所有签名生成问题）。

        真实浏览器的 XHS SDK 会自动附加全部签名头（x-s / x-t / x-s-common /
        x-xray-traceid / x-rap-param），无需本地生成签名。用于 embed-js 签名头
        不完整导致 406 时的兜底。
        """
        import xhs_config
        self._ensure_browser()
        if self._page is None:
            raise SignError("PlaywrightSigner 页面未初始化")
        full_api = api
        if params:
            from urllib.parse import urlencode
            qs = urlencode(params)
            full_api = f"{api}?{qs}" if "?" not in api else f"{api}&{qs}"
        url = xhs_config.BASE + full_api
        body_str = "" if not data else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        js = """
        async ([method, url, body]) => {
            const opts = {method: method, credentials: 'include'};
            if (method === 'POST' && body) {
                opts.headers = {'content-type': 'application/json;charset=UTF-8'};
                opts.body = body;
            }
            try {
                const r = await fetch(url, opts);
                const t = await r.text();
                try { return {ok: true, status: r.status, json: JSON.parse(t)}; }
                catch (e) { return {ok: false, status: r.status, text: t.slice(0, 400)}; }
            } catch (e) { return {ok: false, error: String(e)}; }
        }
        """
        try:
            ret = self._page.evaluate(js, [method, url, body_str])
        except Exception as e:
            raise SignError(f"playwright fetch failed: {e}") from e
        if not ret or not ret.get("ok"):
            raise SignError(f"playwright fetch 返回异常: {ret}")
        return ret.get("json") or {}

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._browser = None
            self._page = None
            self._pw = None


# ---------------------------------------------------------------------------
# PyPortSigner — MVP 占位
# ---------------------------------------------------------------------------

class PyPortSigner(SignerBase):
    """纯 Python 签名端口 — 降级链终点。

    小红书签名算法约每月轮换，纯 Python 维护成本过高。
    此档作为 auto 降级链的最后一环保留，有意不实现。
    实际使用请依赖 embed-js（社区 JS 资产）或 playwright（真实浏览器）。
    """

    name = "py-port"

    def sign(self, api: str, data: Any, a1: str, method: str = "POST",
             platform: str = "") -> dict[str, str]:
        raise SignError(
            "PyPortSigner 是 auto 降级链的终点，有意不实现。"
            "请用 --sign-mode embed-js 或 playwright"
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[SignerBase]] = {
    "embed-js": EmbedJsSigner,
    "playwright": PlaywrightSigner,
    "py-port": PyPortSigner,
}

DEFAULT_CHAIN = ("embed-js", "playwright", "py-port")


class AutoSigner(SignerBase):
    """auto 模式：维持一个 active signer，连续 3 次失败降级到下一档。

    每 50 次请求尝试恢复到首选签名器（避免因临时故障永久降级）。
    """

    name = "auto"
    _RECOVERY_INTERVAL = 50  # 每 N 次签名尝试恢复

    def __init__(self, chain: tuple[str, ...] = DEFAULT_CHAIN) -> None:
        super().__init__()
        self.chain = chain
        self._active_idx = 0
        self._instances: dict[str, SignerBase | None] = {}
        self._sign_count = 0

    def _instance(self, idx: int) -> SignerBase | None:
        if idx >= len(self.chain):
            return None
        name = self.chain[idx]
        if name in self._instances:
            inst = self._instances[name]
            # 如果之前初始化失败，重新尝试（可能是临时问题）
            if inst is None:
                try:
                    inst = _REGISTRY[name]()
                    self._instances[name] = inst
                except SignError:
                    pass
            return inst
        try:
            inst = _REGISTRY[name]()
            self._instances[name] = inst
        except SignError as e:
            print(f"[SIGN] {name} 初始化失败：{e}", file=sys.stderr)
            self._instances[name] = None
        return self._instances[name]

    def _active(self) -> SignerBase:
        while self._active_idx < len(self.chain):
            inst = self._instance(self._active_idx)
            if inst is not None:
                return inst
            self._active_idx += 1
        raise SignError("所有签名档都不可用")

    def _try_recover(self) -> None:
        """定期尝试恢复到首选签名器。"""
        if self._active_idx == 0:
            return
        if self._sign_count % self._RECOVERY_INTERVAL != 0:
            return
        inst = self._instance(0)
        if inst is not None:
            try:
                # 测试一下是否能正常签名
                inst.sign("/test", "", "test_a1", "GET")
                old = self.chain[self._active_idx]
                self._active_idx = 0
                print(f"[SIGN-RECOVER] {old} → {self.chain[0]}（首选签名器已恢复）", file=sys.stderr)
            except SignError:
                pass  # 首选仍然不可用，保持当前档位

    _last_b1_standalone_refresh: float = 0.0  # 上次独立 b1 刷新时间戳（类级变量）

    def _sync_b1(self) -> None:
        """定期从 b1_cache.json 同步到 EmbedJsSigner。"""
        if self._sign_count % 100 != 0:
            return
        emb = self._instances.get("embed-js")
        if isinstance(emb, EmbedJsSigner):
            emb.inject_b1()
        # 如果 PlaywrightSigner 已在运行（作为降级签名器），顺便收割
        pw = self._instances.get("playwright")
        if isinstance(pw, PlaywrightSigner) and pw._page is not None:
            try:
                pw._harvest_b1()
                b1 = pw.get_b1()
                if b1 and isinstance(emb, EmbedJsSigner):
                    emb.inject_b1(b1)
            except Exception:
                pass
        # PlaywrightSigner 不可用时，定期用 headless 浏览器刷新 b1（防止缓存过期 >2h）
        if not (isinstance(pw, PlaywrightSigner) and pw._page is not None):
            _now = time.time()
            _cache_age = _now - (B1_CACHE_PATH.stat().st_mtime if B1_CACHE_PATH.exists() else 0)
            _since_last = _now - AutoSigner._last_b1_standalone_refresh
            if _cache_age > 7200 and _since_last > 3600:  # 缓存 >2h 且距上次刷新 >1h
                AutoSigner._last_b1_standalone_refresh = _now
                print("[SIGN] b1 缓存过期，启动 headless 浏览器刷新...", file=sys.stderr)
                try:
                    new_b1 = extract_b1_standalone()
                    if new_b1 and isinstance(emb, EmbedJsSigner):
                        emb.inject_b1(new_b1, force=True)
                        print(f"[SIGN] b1 独立刷新成功: {new_b1[:16]}...", file=sys.stderr)
                except Exception as e:
                    print(f"[SIGN] b1 独立刷新失败: {e}", file=sys.stderr)

    def sign(self, api: str, data: Any, a1: str, method: str = "POST",
             platform: str = "") -> dict[str, str]:
        self._sign_count += 1
        self._try_recover()
        self._sync_b1()
        signer = self._active()
        try:
            headers = signer.sign(api, data, a1, method, platform=platform)
            signer.record(True)
            self.record(True)
            return headers
        except SignError:
            signer.record(False)
            self.record(False)
            if signer.recent_failures() >= 3:
                old = self.chain[self._active_idx]
                self._active_idx += 1
                if self._active_idx < len(self.chain):
                    new = self.chain[self._active_idx]
                    print(f"[SIGN-DEGRADE] {old} → {new}", file=sys.stderr)
                    return self.sign(api, data, a1, method, platform=platform)
            raise


def make_signer(mode: str = "auto") -> SignerBase:
    if mode == "auto":
        return AutoSigner()
    if mode not in _REGISTRY:
        raise SignError(f"未知 sign-mode: {mode}；可选 {list(_REGISTRY.keys())} 或 auto")
    return _REGISTRY[mode]()


def extract_b1_standalone() -> str | None:
    """启动临时浏览器提取 b1 令牌。用于 Fetcher 遇到 -104 时紧急刷新。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return None
    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        profile = ROOT / "data" / "pw_profile_b1"
        profile.mkdir(parents=True, exist_ok=True)
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()
        page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
        page.wait_for_function("typeof window._webmsxyw === 'function'", timeout=15000)
        b1 = page.evaluate('localStorage.getItem("b1")') or ""
        if b1:
            B1_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = B1_CACHE_PATH.with_suffix('.tmp')
            tmp.write_text(
                json.dumps({"b1": b1, "updated_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(B1_CACHE_PATH)
            print(f"[SIGN] 独立收割 b1: {b1[:16]}...", file=sys.stderr)
            return b1
        return None
    except Exception as e:
        print(f"[SIGN] 独立 b1 收割失败: {e}", file=sys.stderr)
        return None
    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# sign-test 子命令
# ---------------------------------------------------------------------------

def run_sign_test(a1: str | None = None) -> dict[str, bool]:
    """探针：对每档 signer 调用一次 sign()，返回 {name: ok}。"""
    a1 = a1 or "1872a1a000000000000000000000000000"  # 占位 a1，不一定能过真实接口但能验证签名层本身
    api = "/api/sns/web/v1/homefeed"
    data = {"cursor_score": "", "num": 18, "refresh_type": 1, "note_index": 0}
    results: dict[str, bool] = {}
    for name, cls in _REGISTRY.items():
        try:
            signer = cls()
        except SignError as e:
            print(f"[{name}] FAIL  (init) {e}")
            results[name] = False
            continue
        try:
            headers = signer.sign(api, data, a1, "POST")
            ok = bool(headers.get("x-s")) and bool(headers.get("x-t"))
            print(f"[{name}] {'OK' if ok else 'FAIL'}  x-s={headers.get('x-s','')[:24]}... x-t={headers.get('x-t','')}")
            results[name] = ok
        except SignError as e:
            print(f"[{name}] FAIL  (sign) {e}")
            results[name] = False
        finally:
            if hasattr(signer, "close"):
                try:
                    signer.close()
                except Exception:
                    pass
    return results


if __name__ == "__main__":
    run_sign_test()
