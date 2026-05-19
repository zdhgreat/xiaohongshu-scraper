"""JS 签名资产自动更新：从 cv-cat/Spider_XHS 拉取最新签名 JS 并覆盖到 assets/。

用法：
  python scripts/xhs_update_js.py          # 直接调用
  python scripts/xhs.py update-js          # CLI 子命令
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SPIDER_XHS_REPO = "https://github.com/cv-cat/Spider_XHS.git"


def update_js(dry_run: bool = False) -> dict[str, str]:
    """拉取最新签名 JS 并覆盖。

    返回 {文件名: "updated" / "unchanged" / "not_found" / "error"}。
    """
    results: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="xhs_js_") as tmp:
        print(f"[UPDATE-JS] 克隆 {SPIDER_XHS_REPO} ...", file=sys.stderr)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", SPIDER_XHS_REPO, tmp],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            print("[UPDATE-JS] 错误：未找到 git 命令。请安装 git。", file=sys.stderr)
            return {"error": "git not found"}
        except subprocess.CalledProcessError as e:
            print(f"[UPDATE-JS] git clone 失败：{e.stderr[:200]}", file=sys.stderr)
            return {"error": f"git clone failed: {e.stderr[:100]}"}

        # 捕获 git commit hash
        commit_short = "unknown"
        commit_full = ""
        try:
            r = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                commit_short = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if r2.returncode == 0:
                commit_full = r2.stdout.strip()
        except Exception:
            pass

        static_dir = Path(tmp) / "static"
        if not static_dir.exists():
            print("[UPDATE-JS] 仓库中未找到 static/ 目录", file=sys.stderr)
            return {"error": "static/ not found"}

        # 1. xhs_main.js — 取日期最新的那个
        main_candidates = sorted(static_dir.glob("xhs_main_*.js"))
        if main_candidates:
            latest = main_candidates[-1]
            dst = ASSETS / "xhs_main.js"
            if dry_run:
                print(f"[DRY-RUN] {latest.name} → {dst}", file=sys.stderr)
                results["xhs_main.js"] = "dry-run"
            else:
                shutil.copy2(latest, dst)
                # 注入版本注释到 JS 文件头部
                stamp = f"// xhs_main.js | source: {latest.name} | commit: {commit_short} | updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
                original = dst.read_text(encoding="utf-8", errors="ignore")
                # 移除旧的版本注释
                if original.startswith("// xhs_main.js"):
                    original = original.split("\n", 1)[-1]
                dst.write_text(stamp + original, encoding="utf-8")
                print(f"[UPDATE-JS] {latest.name} → xhs_main.js (commit {commit_short})", file=sys.stderr)
                results["xhs_main.js"] = "updated"
        else:
            print("[UPDATE-JS] 未找到 xhs_main_*.js", file=sys.stderr)
            results["xhs_main.js"] = "not_found"

        # 2. xhs_rap.js
        src_rap = static_dir / "xhs_rap.js"
        if src_rap.exists():
            dst = ASSETS / "xhs_rap.js"
            if not dry_run:
                shutil.copy2(src_rap, dst)
            print(f"[UPDATE-JS] xhs_rap.js {'copied' if not dry_run else 'found'}", file=sys.stderr)
            results["xhs_rap.js"] = "updated" if not dry_run else "dry-run"
        else:
            results["xhs_rap.js"] = "not_found"

        # 3. xhs_xray.js
        src_xray = static_dir / "xhs_xray.js"
        if src_xray.exists():
            dst = ASSETS / "xhs_xray.js"
            if not dry_run:
                shutil.copy2(src_xray, dst)
            print(f"[UPDATE-JS] xhs_xray.js {'copied' if not dry_run else 'found'}", file=sys.stderr)
            results["xhs_xray.js"] = "updated" if not dry_run else "dry-run"
        else:
            results["xhs_xray.js"] = "not_found"

    # 记录版本信息到 data/js_version.json
    if not dry_run and commit_short != "unknown":
        version_path = ROOT / "data" / "js_version.json"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(json.dumps({
            "commit_short": commit_short,
            "commit_full": commit_full,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "files": {k: v for k, v in results.items()},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[UPDATE-JS] 版本 {commit_short} 已记录到 data/js_version.json", file=sys.stderr)

    return results


def run_update_js(dry_run: bool = False) -> int:
    """CLI 入口：更新 JS + 验证签名。"""
    results = update_js(dry_run=dry_run)

    if results.get("error"):
        print(f"[FAIL] 更新失败：{results['error']}")
        return 1

    updated = sum(1 for v in results.values() if v == "updated")
    print(f"[OK] 更新完成：{updated} 个文件已覆盖")

    # 验证签名
    print("[UPDATE-JS] 验证签名...", file=sys.stderr)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import xhs_sign
    test_results = xhs_sign.run_sign_test()
    ok_any = any(test_results.values())
    if ok_any:
        print("[OK] 签名验证通过")
    else:
        print("[WARN] 签名验证全失败 — 可能需要等待社区更新 JS 或检查网络")
    return 0 if ok_any else 2


if __name__ == "__main__":
    sys.exit(run_update_js())
