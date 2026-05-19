"""三档签名 + auto 路由 + 失败窗口降级。

签名层产出小红书 web API 必需的请求头：x-s / x-t / x-s-common / x-b3-traceid /
x-xray-traceid / x-rap-param。

三档：
  1. PlaywrightSigner — 真实浏览器跑 window._webmsxyw。免维护算法，最稳。
  2. EmbedJsSigner    — py_mini_racer/execjs 跑 assets/xhs_main.js。快，但 JS 月度轮换需替换文件。
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


def check_js_staleness(warn_days: int = 30, critical_days: int = 60) -> str | None:
    """检查签名 JS 文件是否过期。返回警告消息或 None。"""
    if not XHS_MAIN_JS.exists():
        return None
    age_days = (time.time() - XHS_MAIN_JS.stat().st_mtime) / 86400
    if age_days > critical_days:
        return (f"[WARN] 签名 JS 已 {int(age_days)} 天未更新（>{critical_days}天），"
                f"签名大概率失效！建议: python scripts/xhs.py update-js")
    if age_days > warn_days:
        return (f"[WARN] 签名 JS 已 {int(age_days)} 天未更新（>{warn_days}天），"
                f"建议: python scripts/xhs.py update-js")
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
    def sign(self, api: str, data: Any, a1: str, method: str = "POST") -> dict[str, str]:
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
            # cwd 控制 require('crypto-js') 能否找到 node_modules
            if cwd:
                import os
                old = os.getcwd()
                os.chdir(cwd)
                try:
                    self._ctx = execjs.compile(source)
                finally:
                    os.chdir(old)
            else:
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
        self._engine = _pick_engine()
        self._ctx_main: _JsCtx | None = None
        self._ctx_rap: _JsCtx | None = None
        self._ctx_xray: _JsCtx | None = None
        self._mtime_main: float = 0.0
        self._mtime_rap: float = 0.0
        self._mtime_xray: float = 0.0
        self._reload_if_stale()

    def _reload_if_stale(self) -> None:
        m_main = XHS_MAIN_JS.stat().st_mtime
        if m_main != self._mtime_main:
            self._ctx_main = self._build_ctx(XHS_MAIN_JS)
            self._mtime_main = m_main
            print(f"[SIGN] loaded {XHS_MAIN_JS.name} via {self._engine}", file=sys.stderr)
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

    def _build_ctx(self, js_path: Path) -> "_JsCtx":
        """组装：execjs 路径下把 require('crypto-js') 替换成绝对路径，让 Node 子进程能找到；mini-racer 路径下尝试拼 node-shim + crypto-js（多数 JS 跑不动，仅作降级尝试）。"""
        source = js_path.read_text(encoding="utf-8")
        if self._engine == "execjs":
            # 把 require("crypto-js") 改成项目内绝对路径，避免依赖子进程 cwd
            crypto_node = ASSETS / "node_modules" / "crypto-js"
            if crypto_node.exists():
                abs_path = str(crypto_node).replace("\\", "/")
                source = source.replace('require("crypto-js")', f'require("{abs_path}")')
                source = source.replace("require('crypto-js')", f"require('{abs_path}')")
            return _JsCtx(source, self._engine)
        # mini-racer：勉强尝试，多数情况会失败
        parts: list[str] = [_NODE_SHIM]
        if CRYPTO_JS.exists():
            parts.append(CRYPTO_JS.read_text(encoding="utf-8"))
        parts.append(source)
        return _JsCtx("\n;\n".join(parts), self._engine)

    def sign(self, api: str, data: Any, a1: str, method: str = "POST") -> dict[str, str]:
        self._reload_if_stale()
        assert self._ctx_main is not None
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
            except Exception:
                pass
        if self._ctx_rap:
            try:
                headers["x-rap-param"] = self._ctx_rap.call(
                    "generate_x_rap_param", api, data_str, None
                )
            except Exception:
                pass
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

    def _ensure_browser(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as e:
            raise SignError("需要 playwright。pip install playwright && playwright install chromium") from e
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.persist_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._browser.new_page()
        self._page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
        # 等 _webmsxyw 注入完成
        self._page.wait_for_function("typeof window._webmsxyw === 'function'", timeout=15000)

    def sign(self, api: str, data: Any, a1: str, method: str = "POST") -> dict[str, str]:
        self._ensure_browser()
        assert self._page is not None
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
        return {
            "x-s": ret.get("X-s") or ret.get("x-s") or "",
            "x-t": str(ret.get("X-t") or ret.get("x-t") or ts),
            "x-s-common": ret.get("x-s-common", ""),
            "x-b3-traceid": random_b3_traceid(),
        }

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

    def sign(self, api: str, data: Any, a1: str, method: str = "POST") -> dict[str, str]:
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

    def sign(self, api: str, data: Any, a1: str, method: str = "POST") -> dict[str, str]:
        self._sign_count += 1
        self._try_recover()
        signer = self._active()
        try:
            headers = signer.sign(api, data, a1, method)
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
                    return self.sign(api, data, a1, method)
            raise


def make_signer(mode: str = "auto") -> SignerBase:
    if mode == "auto":
        return AutoSigner()
    if mode not in _REGISTRY:
        raise SignError(f"未知 sign-mode: {mode}；可选 {list(_REGISTRY.keys())} 或 auto")
    return _REGISTRY[mode]()


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
