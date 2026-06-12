"""curl_cffi impersonate 版本探测。

通过实际 HTTP 请求验证 curl_cffi 支持的最高 Chrome 大版本号，
同时提取真实 UA/sec-ch-ua 格式。
"""

from __future__ import annotations

import json
import urllib.request

# 用 httpbin.org 作为探测目标（轻量、返回请求头）
_PROBE_URL = "https://httpbin.org/headers"


def get_installed_version() -> str:
    """返回本地 curl_cffi 版本号；未安装返回 "not installed"。"""
    try:
        import curl_cffi
        return getattr(curl_cffi, "__version__", "unknown")
    except ImportError:
        return "not installed"


def probe_max_chrome_version() -> int:
    """通过实际请求探测 curl_cffi 支持的最高 Chrome 大版本号。

    Session 构造不会验证 impersonate 值，只有实际请求才会报错，
    因此必须发真实请求来验证。兜底返回 136。
    """
    try:
        from curl_cffi.requests import Session
    except ImportError:
        return 136

    for major in range(160, 100, -1):
        try:
            s = Session(impersonate=f"chrome{major}")
            s.get(_PROBE_URL, timeout=10)
            s.close()
            return major
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            continue

    return 136


def probe_real_headers(chrome_major: int) -> dict:
    """用 curl_cffi 实际请求，提取其自动生成的 UA 和 sec-ch-ua。

    返回 {"user_agent": ..., "sec_ch_ua": ...}。
    失败返回空 dict。
    """
    try:
        from curl_cffi.requests import Session
    except ImportError:
        return {}

    try:
        s = Session(impersonate=f"chrome{chrome_major}")
        r = s.get(_PROBE_URL, timeout=10)
        s.close()
        headers = json.loads(r.text).get("headers", {})
        return {
            "user_agent": headers.get("User-Agent", ""),
            "sec_ch_ua": headers.get("Sec-Ch-Ua", ""),
        }
    except Exception:
        return {}
