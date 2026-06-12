"""Chrome 版本查询 + curl_cffi PyPI 版本检查。

从 Google 公开 API 获取 Chrome Stable 版本号，
从 PyPI JSON API 获取 curl_cffi 最新版本号。
"""

from __future__ import annotations

import json
import urllib.request


def get_stable_version() -> int:
    """从 Google API 查询 Chrome Stable 大版本号。

    失败返回 0。
    """
    try:
        url = (
            "https://googlechromelabs.github.io/chrome-for-testing/"
            "last-known-good-versions.json"
        )
        req = urllib.request.urlopen(url, timeout=15)
        data = json.loads(req.read())
        version_str = data["channels"]["Stable"]["version"]
        return int(version_str.split(".")[0])
    except Exception as e:
        print(f"[WARN] Chrome Stable 版本查询失败：{e}", file=__import__("sys").stderr)
        return 0


def check_pypi_curl_cffi() -> tuple[str, str]:
    """查询 PyPI 上 curl_cffi 最新版本。

    返回 (installed_version, latest_version)。
    查询失败时 latest_version 与 installed_version 相同。
    """
    from .cffi_probe import get_installed_version

    installed = get_installed_version()
    try:
        url = "https://pypi.org/pypi/curl_cffi/json"
        req = urllib.request.urlopen(url, timeout=15)
        data = json.loads(req.read())
        latest = data["info"]["version"]
        return installed, latest
    except Exception as e:
        print(f"[WARN] PyPI curl_cffi 版本查询失败：{e}", file=__import__("sys").stderr)
        return installed, installed
