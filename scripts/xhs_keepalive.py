"""多账号 Cookie 自动保活模块。

策略 fallback 链：
  1. validate_cookies_online() → 当前 cookie 仍有效 → 更新并保存
  2. acquire_via_profile_restore() → headless Playwright profile session 恢复
  3. acquire_via_native_browser() → 从用户浏览器提取（跨平台）
  4. 标记短冷却，下次 daemon 循环重试
"""

from __future__ import annotations

import signal
import sys
import time

import xhs_config


def keepalive_single_account(alias: str, acc, force: bool = False) -> str:
    """保活单个账号。返回状态描述字符串。"""
    import xhs_login

    # Tier 1: 检查当前 cookie 是否还有效
    if not force:
        try:
            valid, user_info, updated = xhs_login.validate_cookies_online(
                acc.cookies, fingerprint=getattr(acc, 'fingerprint', None)
            )
        except Exception:
            return "网络异常（跳过）"

        if valid:
            acc.cookies = updated
            acc.save_cookies()
            nickname = (user_info or {}).get("nickname", "")
            return f"有效{f' ({nickname})' if nickname else ''}（已刷新）"

    # Tier 2: Profile Session 恢复
    print(f"  [{alias}] cookie 无效，尝试 Profile 恢复...", file=sys.stderr)
    try:
        new_cookies = xhs_login.acquire_via_profile_restore(alias)
        # 恢复成功后在线验证
        valid, user_info, updated = xhs_login.validate_cookies_online(
            new_cookies, fingerprint=getattr(acc, 'fingerprint', None)
        )
        if valid:
            acc.cookies = updated
            acc.save_cookies()
            nickname = (user_info or {}).get("nickname", "")
            return f"Profile 恢复成功{f' ({nickname})' if nickname else ''}"
        print(f"  [{alias}] Profile 恢复后在线验证失败", file=sys.stderr)
    except xhs_login.LoginError as e:
        print(f"  [{alias}] Profile 恢复失败: {e}", file=sys.stderr)

    # Tier 3: 浏览器 cookie 提取（跨平台）
    print(f"  [{alias}] 尝试从浏览器提取 cookie...", file=sys.stderr)
    try:
        new_cookies = xhs_login.acquire_via_native_browser("edge")
        valid, user_info, updated = xhs_login.validate_cookies_online(
            new_cookies, fingerprint=getattr(acc, 'fingerprint', None)
        )
        if valid:
            acc.cookies = updated
            acc.save_cookies()
            return "浏览器 cookie 提取恢复成功"
    except Exception as e:
        print(f"  [{alias}] 浏览器提取失败: {e}", file=sys.stderr)

    # Tier 4: 全部失败
    acc.cooldown_until = time.time() + xhs_config.KEEPALIVE_FAIL_COOLDOWN_S
    hours = xhs_config.KEEPALIVE_FAIL_COOLDOWN_S // 3600
    return f"自动恢复失败（已设置 {hours}h 冷却，daemon 会重试）"


def keepalive_all(mgr, force: bool = False, account: str | None = None) -> dict[str, str]:
    """保活所有账号或指定账号。返回 {alias: status} 字典。"""
    import xhs_login

    targets = {account: mgr.accounts[account]} if account else mgr.accounts
    results = {}
    for alias, acc in targets.items():
        print(f"[KEEPALIVE] 保活 {alias}...", file=sys.stderr)
        status = keepalive_single_account(alias, acc, force=force)
        results[alias] = status
        print(f"  [{alias:15s}] {status}", file=sys.stderr)
    mgr.save_state()
    return results


def run_daemon(mgr, interval_s: int = 0, single_run: bool = False,
               force: bool = False, account: str | None = None) -> int:
    """运行保活守护进程。single_run=True 时只执行一次。"""
    from datetime import datetime

    interval_s = interval_s or xhs_config.KEEPALIVE_DAEMON_INTERVAL_S

    if single_run:
        results = keepalive_all(mgr, force=force, account=account)
        failed = sum(1 for s in results.values() if "失败" in s)
        return 1 if failed else 0

    # 守护进程模式
    print(f"[KEEPALIVE] 守护进程启动，间隔 {interval_s}s "
          f"（账号: {account or '全部'}）", file=sys.stderr)

    stop = False

    def _signal_handler(sig, frame):
        nonlocal stop
        stop = True
        print(f"\n[KEEPALIVE] 收到信号 {sig}，优雅退出...", file=sys.stderr)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    cycle = 0
    while not stop:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[KEEPALIVE] === 第 {cycle} 轮保活 [{now}] ===", file=sys.stderr)

        results = keepalive_all(mgr, force=force, account=account)

        ok = sum(1 for s in results.values() if "失败" not in s)
        total = len(results)
        print(f"[KEEPALIVE] 本轮结果: {ok}/{total} 个账号有效", file=sys.stderr)

        if stop:
            break

        print(f"[KEEPALIVE] 下一轮在 {interval_s}s 后（{interval_s // 60} 分钟）",
              file=sys.stderr)

        # 分段 sleep，以便快速响应 SIGINT
        waited = 0
        while waited < interval_s and not stop:
            chunk = min(60, interval_s - waited)
            time.sleep(chunk)
            waited += chunk

    print("[KEEPALIVE] 守护进程已停止", file=sys.stderr)
    return 0
