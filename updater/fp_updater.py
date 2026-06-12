"""指纹池全量更新：curl_cffi 探测 → 指纹池生成 → 签名 JS 更新 → 写入配置。

用法：
  python -m updater              # 直接运行
  python scripts/xhs.py update-fp  # CLI 子命令
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cffi_probe import get_installed_version, probe_max_chrome_version, probe_real_headers
from .chrome_versions import check_pypi_curl_cffi, get_stable_version
from .fingerprint_gen import generate_pool

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
VERSION_PATH = DATA_DIR / "fp_version.json"


def _load_existing_config() -> dict:
    """加载现有 config.json，不存在返回空 dict。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_config(cfg: dict) -> None:
    """保存 config.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_version(info: dict) -> None:
    """保存版本追踪文件。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VERSION_PATH.write_text(
        json.dumps(info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _patch_js_navigator(
    chrome_major: int,
    is_edge: bool = True,
    platform_os: str = "Windows",
    dry_run: bool = False,
) -> None:
    """同步 xhs_main.js 中 Navigator mock 的 UA 版本号和 OS/Edge 指纹。

    替换项：
      - Chrome/XXX.0.0.0 → Chrome/{chrome_major}.0.0.0
      - Edg/XXX.0.0.0 → 移除（非 Edge）或保留（Edge）
      - x2: "Windows" → x2: "{platform_os}"（两处）
      - UA 中的 OS 片段（Windows NT / Macintosh; Intel Mac OS X）
    """
    import re

    js_path = ROOT / "assets" / "xhs_main.js"
    if not js_path.exists():
        print("  [SKIP] assets/xhs_main.js 不存在", file=sys.stderr)
        return

    content = js_path.read_text(encoding="utf-8", errors="ignore")
    changes: list[str] = []

    # 1. Chrome 版本号
    chrome_pat = re.compile(r'Chrome/\d+\.0\.0\.0')
    old_chrome = chrome_pat.findall(content)
    if old_chrome:
        old_ver = old_chrome[0]
        new_ver = f"Chrome/{chrome_major}.0.0.0"
        if old_ver != new_ver:
            changes.append(f"{old_ver} → {new_ver}（{len(old_chrome)} 处）")
            content = chrome_pat.sub(new_ver, content)

    # 2. Edge 后缀：非 Edge 时移除 Edg/XXX.0.0.0
    if not is_edge:
        edge_pat = re.compile(r'\s+Edg/\d+\.\d+\.\d+\.\d+')
        edge_matches = edge_pat.findall(content)
        if edge_matches:
            changes.append(f"移除 Edg 后缀（{len(edge_matches)} 处）")
            content = edge_pat.sub('', content)

    # 3. x2 字段
    if platform_os != "Windows":
        x2_pat = re.compile(r'x2:\s*"Windows"')
        x2_matches = x2_pat.findall(content)
        if x2_matches:
            changes.append(f'x2: "Windows" → x2: "{platform_os}"（{len(x2_matches)} 处）')
            content = x2_pat.sub(f'x2: "{platform_os}"', content)

    # 4. UA 中的 OS 片段
    if platform_os == "macOS":
        # Windows NT 10.0; Win64; x64 → Macintosh; Intel Mac OS X 10_15_7
        win_pat = re.compile(r'Windows NT \d+\.\d+; Win64; x64')
        win_matches = win_pat.findall(content)
        if win_matches:
            changes.append(f"OS 片段: Windows → macOS（{len(win_matches)} 处）")
            content = win_pat.sub('Macintosh; Intel Mac OS X 10_15_7', content)

    if not changes:
        print("  已是最新，无需同步", file=sys.stderr)
        return

    if dry_run:
        for c in changes:
            print(f"  [DRY-RUN] {c}", file=sys.stderr)
        return

    js_path.write_text(content, encoding="utf-8")
    for c in changes:
        print(f"  {c}", file=sys.stderr)


def run(dry_run: bool = False) -> bool:
    """执行全量更新。返回 True 成功，False 失败。"""

    print("=" * 50, file=sys.stderr)
    print("[UPDATER] 开始全量更新", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # ---- Layer 1: curl_cffi 探测 ----
    print("\n[Layer 1] curl_cffi 版本探测...", file=sys.stderr)
    cffi_installed, cffi_latest = check_pypi_curl_cffi()
    max_chrome = probe_max_chrome_version()
    print(f"  curl_cffi: 已安装 {cffi_installed}, PyPI 最新 {cffi_latest}", file=sys.stderr)
    print(f"  支持的最高 impersonate: chrome{max_chrome}", file=sys.stderr)

    if cffi_latest != cffi_installed:
        print(
            f"  [HINT] pip install --upgrade curl_cffi "
            f"可升级到 {cffi_latest}（可能解锁更高 Chrome 版本）",
            file=sys.stderr,
        )

    # ---- Layer 2: Chrome Stable 查询 ----
    print("\n[Layer 2] Chrome 版本查询...", file=sys.stderr)
    chrome_stable = get_stable_version()
    if chrome_stable:
        print(f"  Chrome Stable: {chrome_stable}", file=sys.stderr)
    else:
        print("  Chrome Stable: 查询失败，跳过", file=sys.stderr)

    if chrome_stable and chrome_stable > max_chrome:
        print(
            f"  [INFO] Chrome Stable ({chrome_stable}) > curl_cffi 上限 ({max_chrome})",
            file=sys.stderr,
        )
        print(
            "         UA 将使用 curl_cffi 上限版本以保证 TLS↔UA 一致性",
            file=sys.stderr,
        )

    # ---- Layer 3: 提取 curl_cffi 真实 sec-ch-ua ----
    print(f"\n[Layer 3] 提取 curl_cffi 真实请求头...", file=sys.stderr)
    real_headers = probe_real_headers(max_chrome)
    real_sec_ch_ua = real_headers.get("sec_ch_ua", "")
    if real_sec_ch_ua:
        print(f"  真实 sec-ch-ua: {real_sec_ch_ua}", file=sys.stderr)
    else:
        print("  提取失败，使用模板生成", file=sys.stderr)

    # ---- Layer 4: 指纹池生成 ----
    print(f"\n[Layer 4] 生成指纹池（基于 chrome{max_chrome}）...", file=sys.stderr)
    pool = generate_pool(max_chrome, real_sec_ch_ua=real_sec_ch_ua)
    print(f"  生成 {len(pool)} 个指纹 profile", file=sys.stderr)

    if dry_run:
        print("\n[DRY-RUN] 指纹池预览（前 3 个）：", file=sys.stderr)
        for i, fp in enumerate(pool[:3]):
            print(f"  [{i}] UA: ...{fp['user_agent'][-40:]}", file=sys.stderr)
            print(f"      sec-ch-ua: {fp['sec_ch_ua'][:50]}...", file=sys.stderr)
            print(f"      lang: {fp['accept_language']}", file=sys.stderr)
        print(f"  ... 共 {len(pool)} 个", file=sys.stderr)

    # ---- Layer 5: 签名 JS 更新 ----
    print("\n[Layer 5] 签名 JS 更新...", file=sys.stderr)
    js_commit = "unknown"
    js_results = {}
    try:
        # 将 scripts/ 加入 path 以便导入 xhs_update_js
        scripts_dir = ROOT / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import xhs_update_js

        js_results = xhs_update_js.update_js(dry_run=dry_run)
        js_commit = js_results.get("commit", "unknown")
        updated_count = sum(1 for v in js_results.values() if v == "updated")
        print(f"  JS 更新结果：{updated_count} 个文件已更新", file=sys.stderr)
        print("  [INFO] embed-js 为降级后备签名器，主导签名器为 Playwright（真实浏览器）",
              file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] JS 更新失败：{e}", file=sys.stderr)

    # ---- 写入配置 ----
    if dry_run:
        print("\n[DRY-RUN] 跳过写入", file=sys.stderr)
        return True

    print("\n[写入] 更新 data/config.json...", file=sys.stderr)
    cfg = _load_existing_config()

    # 保留已有配置项，只覆盖指纹相关字段
    cfg["user_agent"] = pool[0]["user_agent"]
    cfg["sec_ch_ua"] = pool[0]["sec_ch_ua"]
    cfg["impersonate_profile"] = f"chrome{max_chrome}"
    cfg["fingerprint_pool"] = pool

    _save_config(cfg)
    print(f"  已写入 {len(pool)} 个指纹 profile", file=sys.stderr)

    # ---- 同步 xhs_main.js Navigator 版本号 ----
    print("\n[同步] xhs_main.js Navigator 版本号...", file=sys.stderr)
    # 自动检测运行平台，不再硬编码 Windows
    _platform_os = "macOS" if sys.platform == "darwin" else "Windows"
    print(f"  检测平台: {_platform_os}", file=sys.stderr)
    # 指纹池仅生成 Chrome UA，基线 JS 不应含 Edge 后缀
    _patch_js_navigator(max_chrome, is_edge=False, platform_os=_platform_os,
                        dry_run=dry_run)

    # 版本追踪
    version_info = {
        "curl_cffi_installed": cffi_installed,
        "curl_cffi_latest_pypi": cffi_latest,
        "impersonate_max": max_chrome,
        "chrome_stable": chrome_stable or "unknown",
        "fingerprint_count": len(pool),
        "js_commit": js_commit,
        "js_files": {k: v for k, v in js_results.items()
                     if k not in ("error", "commit")},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_version(version_info)
    print(f"  版本追踪已写入 data/fp_version.json", file=sys.stderr)

    print("\n" + "=" * 50, file=sys.stderr)
    print("[UPDATER] 更新完成", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="XHS 指纹池全量更新")
    parser.add_argument("--dry-run", action="store_true", help="只检查不写入")
    args = parser.parse_args()
    ok = run(dry_run=args.dry_run)
    sys.exit(0 if ok else 1)
