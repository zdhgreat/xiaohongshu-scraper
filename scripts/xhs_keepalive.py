"""多账号 Cookie 自动保活模块。

策略 fallback 链：
  1. validate_cookies_online() → 当前 cookie 仍有效 → 更新并保存
  1.5 acquire_via_rookiepy() → 从本地浏览器提取 cookie（无需 Playwright）
  2. acquire_via_profile_restore() → headless Playwright profile session 恢复
  3. acquire_via_native_browser() → 多浏览器提取（Edge/Chrome/Brave/Firefox）
  4. 标记短冷却，下次 daemon 循环重试
"""

from __future__ import annotations

import random
import signal
import sys
import time

import xhs_config


def keepalive_single_account(alias: str, acc, force: bool = False) -> tuple[bool, str]:
    """保活单个账号。返回 (success, status_description)。"""
    import xhs_login

    # Tier 1: 检查当前 cookie 是否还有效
    if not force:
        try:
            valid, user_info, updated = xhs_login.validate_cookies_online(
                acc.cookies, fingerprint=getattr(acc, 'fingerprint', None)
            )
        except Exception:
            # 网络异常不跳过降级链，继续尝试 Tier 1.5（不需要网络）
            valid = None
            print(f"  [{alias}] Tier 1 网络异常，继续降级尝试...", file=sys.stderr)

        if valid:
            acc.cookies = updated
            acc.save_cookies()
            acc.record_validation()
            nickname = (user_info or {}).get("nickname", "")
            # Tier 1 b1 收割：保活时顺便尝试收割 b1
            try:
                import xhs_sign
                b1 = xhs_sign.extract_b1_standalone()
                if b1:
                    print(f"  [{alias}] b1 收割成功: {b1[:16]}...", file=sys.stderr)
            except Exception:
                pass  # b1 收割失败不影响保活
            return True, f"有效{f' ({nickname})' if nickname else ''}（已刷新）"

    # Tier 1.5: rookiepy 浏览器提取（无需 Playwright）
    print(f"  [{alias}] cookie 无效，尝试 rookiepy 浏览器提取...", file=sys.stderr)
    try:
        new_cookies, new_meta = xhs_login.acquire_via_rookiepy()
        if new_cookies:
            valid, user_info, updated = xhs_login.validate_cookies_online(
                new_cookies, fingerprint=getattr(acc, 'fingerprint', None)
            )
            if valid:
                acc.cookies = updated
                acc.cookie_meta = new_meta
                acc.save_cookies()
                acc.record_validation()
                nickname = (user_info or {}).get("nickname", "")
                return True, f"rookiepy 恢复成功{f' ({nickname})' if nickname else ''}"
            print(f"  [{alias}] rookiepy 提取后在线验证失败", file=sys.stderr)
    except Exception as e:
        print(f"  [{alias}] rookiepy 提取失败: {e}", file=sys.stderr)

    # Tier 2: Profile Session 恢复
    print(f"  [{alias}] cookie 无效，尝试 Profile 恢复...", file=sys.stderr)
    try:
        new_cookies, new_meta = xhs_login.acquire_via_profile_restore(alias)
        # 恢复成功后在线验证
        valid, user_info, updated = xhs_login.validate_cookies_online(
            new_cookies, fingerprint=getattr(acc, 'fingerprint', None)
        )
        if valid:
            acc.cookies = updated
            acc.cookie_meta = new_meta
            acc.save_cookies()
            acc.record_validation()  # Tier 2 也要更新验证状态
            nickname = (user_info or {}).get("nickname", "")
            return True, f"Profile 恢复成功{f' ({nickname})' if nickname else ''}"
        print(f"  [{alias}] Profile 恢复后在线验证失败", file=sys.stderr)
    except xhs_login.LoginError as e:
        print(f"  [{alias}] Profile 恢复失败: {e}", file=sys.stderr)

    # Tier 3: 多浏览器 cookie 提取（Edge/Chrome/Brave/Firefox）
    print(f"  [{alias}] 尝试从浏览器提取 cookie...", file=sys.stderr)
    for browser in ["edge", "chrome", "brave"]:
        try:
            new_cookies, new_meta = xhs_login.acquire_via_native_browser(browser)
            valid, user_info, updated = xhs_login.validate_cookies_online(
                new_cookies, fingerprint=getattr(acc, 'fingerprint', None)
            )
            if valid:
                acc.cookies = updated
                acc.cookie_meta = new_meta
                acc.save_cookies()
                acc.record_validation()  # Tier 3 也要更新验证状态
                return True, f"{browser} 浏览器 cookie 提取恢复成功"
        except Exception as e:
            print(f"  [{alias}] {browser} 浏览器提取失败: {e}", file=sys.stderr)

    # Tier 4: 全部失败
    acc.cooldown_until = time.time() + xhs_config.KEEPALIVE_FAIL_COOLDOWN_S
    hours = xhs_config.KEEPALIVE_FAIL_COOLDOWN_S // 3600
    return False, f"自动恢复失败（已设置 {hours}h 冷却，daemon 会重试）"


def keepalive_all(mgr, force: bool = False, account: str | None = None) -> dict[str, tuple[bool, str]]:
    """保活所有账号或指定账号。返回 {alias: (success, status)} 字典。"""
    targets = {account: mgr.accounts[account]} if account else mgr.accounts
    results = {}
    for alias, acc in list(targets.items()):
        print(f"[KEEPALIVE] 保活 {alias}...", file=sys.stderr)
        success, status = keepalive_single_account(alias, acc, force=force)
        results[alias] = (success, status)
        icon = "OK" if success else "FAIL"
        print(f"  [{alias:15s}] [{icon}] {status}", file=sys.stderr)
    mgr.save_state()
    return results


def run_daemon(mgr, interval_s: int = 0, single_run: bool = False,
               force: bool = False, account: str | None = None) -> int:
    """运行保活守护进程。single_run=True 时只执行一次。"""
    from datetime import datetime

    interval_s = interval_s or xhs_config.KEEPALIVE_DAEMON_INTERVAL_S

    if single_run:
        results = keepalive_all(mgr, force=force, account=account)
        failed = sum(1 for s, _ in results.values() if not s)
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

        ok = sum(1 for s, _ in results.values() if s)
        total = len(results)
        print(f"[KEEPALIVE] 本轮结果: {ok}/{total} 个账号有效", file=sys.stderr)

        if stop:
            break

        # 自适应间隔：低健康分账号缩短间隔以更快恢复
        avg_health = 1.0
        if mgr.accounts:
            scores = [a.health_score for a in mgr.accounts.values()]
            avg_health = sum(scores) / len(scores)
        if avg_health < 0.5:
            base = interval_s // 2
            reason = f"低健康分 avg={avg_health:.2f}，间隔减半"
        else:
            base = interval_s
            reason = ""
        # 随机抖动：基准间隔 ± (-10min, +20min)，避免固定周期
        jitter = random.uniform(-600, 1200)
        wait = max(base + jitter, 1800)  # 至少等 30 分钟
        print(f"[KEEPALIVE] 等待 {wait/60:.0f} 分钟后开始下一轮"
              f"{f'（{reason}）' if reason else ''}", file=sys.stderr)
        time.sleep(wait)
