"""小红书爬虫 CLI 入口。

子命令：login / sign-test / note / user / search / export / ...

本文件仅保留 CLI 调度层（argparse + cmd_* handlers）。
核心逻辑已拆分到：
  xhs_config   — 统一配置 + 路径 + 共享工具
  xhs_fetcher  — Fetcher + PlaywrightTakeover + 错误处理
  xhs_api      — API 函数 + 数据标准化
  xhs_media    — 媒体下载 + 后处理
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import xhs_accounts
import xhs_bootstrap
import xhs_config
import xhs_log
import xhs_login
import xhs_media
import xhs_proxy
import xhs_sign
import xhs_storage

# 从拆分模块导入核心类和函数
from xhs_fetcher import Fetcher, FatalRiskError
from xhs_api import (
    fetch_note_detail, fetch_user_info, fetch_user_notes,
    fetch_search, fetch_feed, fetch_comments, fetch_sub_comments,
    _normalize_note, _normalize_user, _normalize_comment,
)

# 从子模块导入 CLI 命令处理器
from xhs_analyze import cmd_analyze
from xhs_bootstrap import cmd_setup, cmd_setup_wizard
from xhs_image import cmd_analyze_images, cmd_setup_image
from xhs_video import cmd_analyze_video, cmd_setup_video


# ---------------------------------------------------------------------------
# 心跳线程：防止HTTP连接静默超时
# ---------------------------------------------------------------------------

class _Heartbeat:
    """后台守护线程，定期输出到stderr防止HTTP连接被服务端判定为静默。
    适用于 Kimi/国产API 等 60s 静默超时的场景。"""
    def __init__(self, interval: float = 15.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            print("[heartbeat] 任务仍在运行...", file=sys.stderr, flush=True)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _try_upsert_user_from_note(note: dict, conn) -> None:
    """从笔记的 raw 数据提取用户信息并 upsert（解决搜索/推荐流中无 user 信息的问题）。"""
    user_id = note.get("user_id")
    if not user_id:
        return
    raw = note.get("raw") or note.get("raw_json")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:
            return
    if not isinstance(raw, dict):
        return
    user_info = raw.get("user") or {}
    if not user_info:
        return
    user_info["user_id"] = user_id
    xhs_storage.upsert_user(conn, _normalize_user(user_info))


def _type_tag(note: dict) -> str:
    """生成类型标签: [图文·9图] / [视频]。"""
    if note.get("type") == "video":
        return "视频"
    raw = note.get("raw") or note.get("raw_json")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:
            raw = {}
    if isinstance(raw, dict):
        n_img = len(raw.get("image_list") or [])
        if n_img:
            return f"图文·{n_img}图"
    return "图文"


def _desc_preview(note: dict, max_len: int = 50) -> str:
    """生成描述预览: 标题后追加描述前 N 字。"""
    title = note.get("title") or ""
    desc = note.get("description") or ""
    if not title and desc:
        return desc[:max_len]
    if title and desc and desc != title:
        extra = desc[:max_len].replace("\n", " ")
        return f"{title} | {extra}{'...' if len(desc) > max_len else ''}"
    return title


# ---------------------------------------------------------------------------
# Fetcher 工厂
# ---------------------------------------------------------------------------

def _validate_accounts(mgr: xhs_accounts.AccountManager) -> None:
    """启动预检：在线验证所有账号 cookie，无效的标记 24h 冷却。"""
    if not mgr.has_accounts():
        return
    print(f"[CLI] 启动预检：验证 {len(mgr.accounts)} 个账号的 cookie ...", file=sys.stderr)
    for alias, acc in list(mgr.accounts.items()):
        try:
            valid, user_info, updated_cookies = xhs_login.validate_cookies_online(
                acc.cookies, fingerprint=getattr(acc, 'fingerprint', None))
        except Exception:
            print(f"  [{alias:15s}] 网络异常（不跳过）", file=sys.stderr)
            continue
        if valid:
            acc.cookies = updated_cookies
            acc.save_cookies()
            nickname = (user_info or {}).get("nickname", "")
            print(f"  [{alias:15s}] 有效{f' ({nickname})' if nickname else ''}", file=sys.stderr)
        else:
            acc.mark_invalid()
            print(f"  [{alias:15s}] 已过期（已跳过）", file=sys.stderr)
    mgr.save_state()


def _make_fetcher(args: argparse.Namespace) -> Fetcher:
    # 1. 账号
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("[CLI] 未找到任何账号，开始登录到 default...", file=sys.stderr)
        cookies = xhs_login.acquire_cookies(prefer="auto")
        xhs_login.persist_cookies(cookies)
        # 重新加载
        mgr = xhs_accounts.AccountManager()
    # 1.5 启动预检：在线验证所有账号 cookie
    _validate_accounts(mgr)
    force = getattr(args, "account", None)
    # 2. 代理池
    proxies_arg = getattr(args, "proxy", None)
    proxy_pool = xhs_proxy.ProxyPool([proxies_arg] if proxies_arg else None)
    if proxy_pool.is_active():
        print(f"[CLI] 代理池启用：{len(proxy_pool)} 个", file=sys.stderr)
    # 3. signer / speed
    signer = xhs_sign.make_signer(args.sign_mode)
    speed = xhs_config.SPEED_PROFILES[args.speed_mode]
    return Fetcher(signer, speed, mgr, proxy_pool,
                    force_account=force,
                    sign_mode_label=args.sign_mode,
                    speed_mode_label=args.speed_mode)


# ---------------------------------------------------------------------------
# fetch_session: 统一 fetcher + conn 生命周期管理
# ---------------------------------------------------------------------------

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator

@contextmanager
def fetch_session(args) -> Generator[tuple[Fetcher, sqlite3.Connection], None, None]:
    """统一管理 fetcher 和 DB conn 的创建/清理，消除 cmd_* 中的 try/finally 样板。"""
    import sqlite3
    fetcher = _make_fetcher(args)
    conn = xhs_storage.connect()
    try:
        yield fetcher, conn
    finally:
        fetcher.close()
        conn.close()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> int:
    name = getattr(args, "name", None) or ""
    cookies = xhs_login.acquire_cookies(prefer=args.prefer, headless_qr=False, profile_hint=name)
    if args.name and args.name != "default":
        # 多账号：落到 data/accounts/<name>.json
        xhs_config.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        path = xhs_config.ACCOUNTS_DIR / f"{args.name}.json"
        path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        xhs_config.restrict_file(path)
        print(f"[OK] 已保存 {len(cookies)} 个 cookie 到 {path}（账号别名: {args.name}）")
    else:
        xhs_login.persist_cookies(cookies)
        print(f"[OK] 已保存 {len(cookies)} 个 cookie 到 {xhs_config.COOKIES_PATH}")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("无账号。先跑 `login` 或 `login --name <alias>`。")
        return 0
    print(f"=== 共 {len(mgr.accounts)} 个账号 ===")
    for s in mgr.stats():
        cd = f"cooldown→{s['cooldown_until']}" if s['cooldown_until'] else ""
        print(f"  [{s['alias']:15s}] 日抓 {s['daily_count']:3d}/{xhs_config.DAILY_HARD_CAP}  累计 {s['total_calls']:5d}"
              f"  460×{s['last_460']}  461×{s['last_461']}  最近用 {s['last_used'] or '从未'}  {cd}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    s = xhs_log.stats(hours=args.hours, account=args.account)
    xhs_log.print_stats(s)
    return 0


def cmd_sign_test(args: argparse.Namespace) -> int:
    cookies = xhs_login.load_cookies() or {}
    a1 = cookies.get("a1")
    results = xhs_sign.run_sign_test(a1=a1)
    ok_any = any(results.values())
    return 0 if ok_any else 1


def cmd_update_js(args: argparse.Namespace) -> int:
    import xhs_update_js
    return xhs_update_js.run_update_js(dry_run=getattr(args, "dry_run", False))


def _refresh_single_account(alias: str, acc: xhs_accounts.Account, force: bool = False) -> str:
    """刷新单个账号的 cookie。返回状态描述。"""
    valid, user_info, updated_cookies = xhs_login.validate_cookies_online(
        acc.cookies, fingerprint=getattr(acc, 'fingerprint', None))
    if valid and not force:
        # 有效且非强制 → 更新 Set-Cookie 并保存
        acc.cookies = updated_cookies
        acc.save_cookies()
        nickname = (user_info or {}).get("nickname", "")
        return f"有效{f' ({nickname})' if nickname else ''}（已刷新服务端 cookie）"

    # cookie 无效或强制刷新 → 尝试重新登录
    # 多账号时不能用 rookiepy（会从浏览器提取到当前登录账号的 cookie，而非目标账号的）
    print(f"  [{alias}] cookie {'已失效' if not valid else '强制刷新'}，尝试重新登录...", file=sys.stderr)
    try:
        new_cookies = xhs_login.acquire_cookies(prefer="qr", profile_hint=alias)
        acc.cookies = new_cookies
        acc.save_cookies()
        return "已重新登录（QR 扫码）"
    except xhs_login.LoginError as e:
        # QR 失败 → 报告需手动处理
        return f"自动登录失败（{e}），需手动登录：python scripts/xhs.py login --name {alias}"


def cmd_refresh_cookies(args: argparse.Namespace) -> int:
    """批量检查并刷新所有账号的 cookie。"""
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("无账号。先跑 `login` 或 `login --name <alias>`。")
        return 0

    force = getattr(args, "force", False)
    print(f"=== 检查 {len(mgr.accounts)} 个账号的 cookie ===")
    for alias, acc in mgr.accounts.items():
        status = _refresh_single_account(alias, acc, force=force)
        print(f"  [{alias:15s}] {status}")
    print("[OK] cookie 刷新完成")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    with fetch_session(args) as (fetcher, conn):
        # 优先用 CLI 显式提供的 token；否则查 DB（之前 search/user 入库时存的）
        xsec_token = args.xsec_token or ""
        xsec_source = "pc_search"
        if not xsec_token:
            row = xhs_storage.get_note(conn, args.note_id)
            if row and row["xsec_token"]:
                xsec_token = row["xsec_token"]
                xsec_source = row["xsec_source"] or "pc_search"
                print(f"[NOTE] 复用 DB 里 {args.note_id} 的 xsec_token（来源 {xsec_source}）",
                      file=sys.stderr)
        if not xsec_token:
            print("[NOTE] ⚠️ 无 xsec_token，直访 detail 极易触发 461。建议先跑 search/user 获取。",
                  file=sys.stderr)

        item = fetch_note_detail(fetcher, args.note_id, xsec_token=xsec_token, xsec_source=xsec_source)
        note = _normalize_note(item)
        if not note["note_id"]:
            note["note_id"] = args.note_id
        if not note.get("xsec_token") and xsec_token:
            note["xsec_token"] = xsec_token
            note["xsec_source"] = xsec_source
        xhs_storage.upsert_note(conn, note)
        if note["user_id"]:
            user = (item.get("note_card") or {}).get("user") or {}
            user["user_id"] = note["user_id"]
            xhs_storage.upsert_user(conn, _normalize_user(user))
        conn.commit()
        path = xhs_storage.write_markdown(conn, note["note_id"])
        title = note.get("title") or note.get("description", "")[:30] or "(无标题)"
        print(f"[OK] 笔记入库并渲染: 《{title}》")
        print(f"     文件: {path}")
        # 自动下载图片
        xhs_media.auto_download_note(note, conn)
        summary = xhs_storage.render_update_summary(conn, note["note_id"])
        if summary:
            print(f"     状态: {summary}")
        return 0


def cmd_user(args: argparse.Namespace) -> int:
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            info = fetch_user_info(fetcher, args.user_id)
            if info:
                info["user_id"] = args.user_id
                xhs_storage.upsert_user(conn, _normalize_user(info))
                conn.commit()
                nickname = info.get('nickname', '')
                fans = info.get('fans', '?')
                print(f"[OK] 用户: {nickname}（粉丝 {fans}）")

            cursor = ""
            total = 0
            for page in range(1, args.pages + 1):
                data = fetch_user_notes(fetcher, args.user_id, cursor=cursor)
                items = data.get("notes") or []
                if not items:
                    break
                for item in items:
                    # user_posted 接口的 token 来源
                    item.setdefault("xsec_source", "pc_user")
                    note = _normalize_note({"id": item.get("note_id") or item.get("id"),
                                            "xsec_token": item.get("xsec_token", ""),
                                            "xsec_source": "pc_user",
                                            "note_card": item})
                    if not note["note_id"]:
                        continue
                    note["user_id"] = args.user_id
                    xhs_storage.upsert_note(conn, note)
                    _try_upsert_user_from_note(note, conn)
                    total += 1
                    token_mark = "Y" if note.get("xsec_token") else "N"
                    tag = _type_tag(note)
                    preview = _desc_preview(note)
                    print(f"  [{total}] [{tag}] {note['note_id']} token={token_mark} {preview}")
                    # --download / --analyze 后处理
                    xhs_media.post_process_note(note, conn, args)
                conn.commit()
                cursor = data.get("cursor", "")
                if not data.get("has_more"):
                    break
            nickname = info.get('nickname', '') if info else ''
            label = f"《{nickname}》" if nickname else args.user_id
            print(f"[OK] {label} 共入库 {total} 条笔记")
            return 0
    finally:
        hb.stop()


def cmd_search(args: argparse.Namespace) -> int:
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            total = 0
            for page in range(1, args.pages + 1):
                data = fetch_search(fetcher, args.keyword, page=page)
                items = data.get("items") or []
                if not items:
                    break
                note_ids: list[str] = []
                for item in items:
                    if item.get("model_type") != "note":
                        continue
                    # 标记 xsec_source（search 接口的 token 来源是 pc_search）
                    item.setdefault("xsec_source", "pc_search")
                    note = _normalize_note(item)
                    if not note["note_id"]:
                        continue
                    xhs_storage.upsert_note(conn, note)
                    _try_upsert_user_from_note(note, conn)
                    note_ids.append(note["note_id"])
                    total += 1
                    token_mark = "Y" if note.get("xsec_token") else "N"
                    tag = _type_tag(note)
                    preview = _desc_preview(note)
                    print(f"  [{total}] [{tag}] {note['note_id']} token={token_mark} {preview}")
                    # --download / --analyze 后处理
                    xhs_media.post_process_note(note, conn, args)
                xhs_storage.save_search_page(conn, args.keyword, page, note_ids)
                conn.commit()
                if not data.get("has_more"):
                    break
            print(f"[OK] 关键词「{args.keyword}」共入库 {total} 条笔记")
            return 0
    finally:
        hb.stop()


def cmd_comments(args: argparse.Namespace) -> int:
    """抓某笔记的评论树。会复用 DB 里存的 xsec_token。"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            row = xhs_storage.get_note(conn, args.note_id)
            if not row:
                print(f"[ERR] 笔记 {args.note_id} 不在 DB，请先跑 note/search 入库", file=sys.stderr)
                return 1
            xsec_token = row["xsec_token"] or ""
            if not xsec_token:
                print(f"[ERR] 笔记 {args.note_id} 缺 xsec_token", file=sys.stderr)
                return 1

            # 1) 主评论分页
            cursor = ""
            main_count = 0
            sub_count = 0
            max_pages = args.max_pages
            for page in range(1, max_pages + 1):
                data = fetch_comments(fetcher, args.note_id, xsec_token, cursor=cursor)
                comments = data.get("comments") or []
                if not comments:
                    break
                for c in comments:
                    norm = _normalize_comment(c, args.note_id)
                    if not norm["comment_id"]:
                        continue
                    xhs_storage.upsert_comment(conn, norm)
                    main_count += 1
                    # 内联的 sub_comments
                    for sc in (c.get("sub_comments") or []):
                        sn = _normalize_comment(sc, args.note_id, parent_id=norm["comment_id"])
                        if sn["comment_id"]:
                            xhs_storage.upsert_comment(conn, sn)
                            sub_count += 1
                    # 还有更多子评论 → 分页拉
                    if c.get("sub_comment_has_more") and args.with_sub:
                        sub_cursor = c.get("sub_comment_cursor", "")
                        sub_pages = 0
                        while sub_cursor and sub_pages < args.max_sub_pages:
                            sub_data = fetch_sub_comments(
                                fetcher, args.note_id, norm["comment_id"], xsec_token,
                                cursor=sub_cursor,
                            )
                            subs = sub_data.get("comments") or []
                            if not subs:
                                break
                            for sc in subs:
                                sn = _normalize_comment(sc, args.note_id, parent_id=norm["comment_id"])
                                if sn["comment_id"]:
                                    xhs_storage.upsert_comment(conn, sn)
                                    sub_count += 1
                            sub_cursor = sub_data.get("cursor", "")
                            if not sub_data.get("has_more"):
                                break
                            sub_pages += 1
                print(f"  [page {page}] +{len(comments)} 主, 累计 {main_count}/{sub_count} 主/子",
                      file=sys.stderr)
                conn.commit()   # 每页结束立即提交，缩短事务时间
                cursor = data.get("cursor", "")
                if not data.get("has_more") or not cursor:
                    break

            conn.commit()  # 最终确保所有数据落盘
            print(f"[OK] 笔记 {args.note_id}: {main_count} 主评论 + {sub_count} 子评论入库")
            # 重新渲染 MD（带评论区）
            title = row["title"] or "(无标题)"
            path = xhs_storage.write_markdown(conn, args.note_id)
            print(f"     《{title}》MD 已更新: 新增 {main_count} 主评论 + {sub_count} 子评论")
            print(f"     文件: {path}")
            return 0
    finally:
        hb.stop()


def cmd_health(args: argparse.Namespace) -> int:
    """系统健康检查：依赖、签名、账号、数据库。返回码 0=健康, 1=降级, 2=严重。"""
    issues: list[tuple[int, str]] = []  # (severity, message)

    # 1. Python 核心依赖
    print("=== Python 依赖 ===")
    core_deps = [
        ("curl_cffi", "curl_cffi", "反风控 Chrome TLS 模拟"),
        ("execjs", "PyExecJS", "签名引擎"),
        ("cryptography", "cryptography", "WSL cookie 解密"),
    ]
    for mod_name, display_name, feature in core_deps:
        try:
            __import__(mod_name)
            print(f"  OK   {display_name:30s} {feature}")
        except ImportError:
            print(f"  MISS {display_name:30s} {feature}")
            issues.append((1, f"{display_name} 未安装 → {feature}"))

    # 2. Node.js + crypto-js
    print("\n=== Node.js + 签名依赖 ===")
    import xhs_bootstrap
    node_path = xhs_bootstrap._find_node()
    if node_path:
        print(f"  OK   {'Node.js':30s} {node_path}")
    else:
        print(f"  CRIT {'Node.js':30s} 未找到")
        issues.append((2, "Node.js 未安装"))
    if xhs_bootstrap._has_crypto_js():
        print(f"  OK   {'crypto-js':30s} 签名算法核心")
    else:
        print(f"  CRIT {'crypto-js':30s} 未找到")
        issues.append((2, "crypto-js 未安装"))

    # 3. 可选依赖
    print("\n=== 可选依赖 ===")
    opt_deps = [
        ("playwright", "Playwright", "QR 登录 / 浏览器接管"),
        ("jieba", "jieba", "本地文本分析"),
        ("rapidocr_onnxruntime", "rapidocr", "图片/视频 OCR"),
    ]
    for mod_name, display_name, feature in opt_deps:
        try:
            __import__(mod_name)
            print(f"  OK   {display_name:30s} {feature}")
        except ImportError:
            print(f"  MISS {display_name:30s} {feature}")
            issues.append((1, f"{display_name} 未安装 → {feature}"))
    # ffmpeg
    import subprocess as _sp
    try:
        _sp.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        print(f"  OK   {'ffmpeg':30s} 视频分析")
    except Exception:
        print(f"  MISS {'ffmpeg':30s} 视频分析（系统级工具）")
        issues.append((1, "ffmpeg 未安装"))

    # 4. 签名探针
    print("\n=== 签名层 ===")
    ver = xhs_sign._load_js_version()
    js_ver = ver.get("commit_short", "未知")
    print(f"  INFO JS 版本: {js_ver}")
    try:
        test_results = xhs_sign.run_sign_test()
        for name, ok in test_results.items():
            status = "OK" if ok else "FAIL"
            print(f"  {status:4s} {name}")
            if not ok:
                issues.append((1, f"签名器 {name} 失败"))
        if not any(test_results.values()):
            issues.append((2, "所有签名器都失败"))
    except Exception as e:
        print(f"  CRIT 签名测试失败: {e}")
        issues.append((2, f"签名层异常: {e}"))

    # 5. 账号状态
    print("\n=== 账号 ===")
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("  CRIT 无账号（先运行 login）")
        issues.append((2, "无账号"))
    else:
        for s in mgr.stats():
            cd = " cooldown" if s["cooldown_until"] else ""
            print(f"  {s['alias']:15s} 日抓 {s['daily_count']:3d}/{xhs_config.DAILY_HARD_CAP}{cd}")
            if s["cooldown_until"]:
                issues.append((1, f"账号 {s['alias']} 冷却中"))

    # 6. 数据库
    print("\n=== 数据库 ===")
    try:
        conn = xhs_storage.connect()
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] == "ok":
                print("  OK   SQLite 完整性检查通过")
                total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
                print(f"  INFO notes: {total} | users: {users} | comments: {comments}")
            else:
                print(f"  CRIT SQLite 完整性异常: {result[0]}")
                issues.append((2, f"数据库损坏: {result[0]}"))
        finally:
            conn.close()
    except Exception as e:
        print(f"  CRIT 数据库连接失败: {e}")
        issues.append((2, f"数据库异常: {e}"))

    # 汇总
    severity = max((sev for sev, _ in issues), default=0)
    labels = {0: "健康", 1: "降级", 2: "严重"}
    print(f"\n=== 结果: {labels[severity]} ({len(issues)} 个问题) ===")
    for sev, msg in issues:
        tag = ["", "WARN", "CRIT"][sev]
        print(f"  [{tag}] {msg}")
    return severity


def cmd_cleanup(args: argparse.Namespace) -> int:
    """数据清理：孤儿媒体、过期缓存、可选 VACUUM。"""
    conn = xhs_storage.connect()
    try:
        dry = getattr(args, "dry_run", False)

        # 1. 孤儿媒体清理
        media_dir = xhs_config.MEDIA_DIR
        orphan_count = 0
        freed_bytes = 0
        if media_dir.exists():
            # 收集所有有效的媒体目录
            valid_paths: set[Path] = set()
            for (note_id,) in conn.execute("SELECT note_id FROM notes").fetchall():
                new_dir = xhs_config.note_media_dir(note_id, conn)
                valid_paths.add(new_dir.resolve())
                valid_paths.add((media_dir / note_id).resolve())

            for child in media_dir.iterdir():
                if not child.is_dir():
                    continue
                resolved = child.resolve()
                if resolved in valid_paths:
                    continue
                # 子目录也检查（新格式 author/title）
                if any(resolved == vp or resolved.is_relative_to(vp) for vp in valid_paths):
                    continue
                size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                orphan_count += 1
                freed_bytes += size
                if dry:
                    print(f"  [DRY-RUN] 将删除: {child.name} ({size / 1024:.0f} KB)")
                else:
                    import shutil
                    shutil.rmtree(child, ignore_errors=True)
            if orphan_count:
                label = "将删除" if dry else "已删除"
                print(f"  孤儿媒体: {label} {orphan_count} 个目录 ({freed_bytes / 1024:.0f} KB)")
            else:
                print("  孤儿媒体: 无")

        # 2. 过期 search_cache
        cache_days = getattr(args, "max_cache_days", 30)
        deleted_cache = 0
        if cache_days > 0:
            try:
                # 删除空记录 + 超龄记录
                deleted_cache += conn.execute(
                    "DELETE FROM search_cache WHERE note_ids = '' OR note_ids = '[]'"
                ).rowcount
                deleted_cache += conn.execute(
                    "DELETE FROM search_cache WHERE crawled_at < datetime('now', ?)",
                    (f"-{cache_days} days",),
                ).rowcount
            except Exception:
                pass
        if dry and deleted_cache:
            print(f"  [DRY-RUN] 将清理 {deleted_cache} 条空搜索缓存")
        elif deleted_cache:
            print(f"  搜索缓存: 已清理 {deleted_cache} 条空记录")

        # 3. 过期 crawl_state
        state_days = getattr(args, "max_state_days", 7)
        if state_days > 0:
            try:
                deleted_state = conn.execute(
                    "DELETE FROM crawl_state WHERE status IN ('completed', 'paused')"
                ).rowcount
                if deleted_state:
                    label = "将清理" if dry else "已清理"
                    print(f"  抓取状态: {label} {deleted_state} 条已完成/暂停记录")
                else:
                    print("  抓取状态: 无需清理")
            except Exception:
                pass

        if not dry:
            conn.commit()

        # 4. VACUUM
        if getattr(args, "vacuum", False) and not dry:
            print("  VACUUM: 正在压缩数据库...")
            conn.execute("VACUUM")
            print("  VACUUM: 完成")
        elif getattr(args, "vacuum", False):
            print("  [DRY-RUN] VACUUM: 将压缩数据库")

        print("[OK] 数据清理完成")
        return 0
    finally:
        conn.close()


def cmd_refresh(args: argparse.Namespace) -> int:
    """重抓超过 N 小时的笔记（增量更新）。"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
            rows = conn.execute(
                "SELECT note_id, xsec_token, xsec_source FROM notes "
                "WHERE crawled_at < ? ORDER BY crawled_at ASC LIMIT ?",
                (cutoff, args.limit),
            ).fetchall()
            if not rows:
                print(f"[OK] 没有 {args.max_age_hours} 小时前的旧笔记需要刷新")
                return 0
            print(f"[REFRESH] 找到 {len(rows)} 条超过 {args.max_age_hours}h 的旧笔记", file=sys.stderr)
            updated = 0
            skipped = 0
            for row in rows:
                note_id = row["note_id"]
                xsec_token = row["xsec_token"] or ""
                xsec_source = row["xsec_source"] or "pc_search"
                try:
                    item = fetch_note_detail(fetcher, note_id, xsec_token=xsec_token, xsec_source=xsec_source)
                    note = _normalize_note(item)
                    note["note_id"] = note_id
                    if not note.get("xsec_token") and xsec_token:
                        note["xsec_token"] = xsec_token
                        note["xsec_source"] = xsec_source
                    # 检查是否被跳过（content_hash 相同）
                    old_hash = conn.execute(
                        "SELECT content_hash FROM notes WHERE note_id = ?", (note_id,)
                    ).fetchone()
                    old_hash_val = old_hash["content_hash"] if old_hash else ""
                    xhs_storage.upsert_note(conn, note)
                    if note["user_id"]:
                        user = (item.get("note_card") or {}).get("user") or {}
                        user["user_id"] = note["user_id"]
                        xhs_storage.upsert_user(conn, _normalize_user(user))
                    conn.commit()
                    if old_hash_val and old_hash_val == note.get("content_hash", ""):
                        skipped += 1
                        print(f"  [{updated + skipped}/{len(rows)}] {note_id} 未变更（跳过）")
                    else:
                        updated += 1
                        title = note.get("title", "")[:40]
                        print(f"  [{updated + skipped}/{len(rows)}] {note_id} 已更新: 《{title}》")
                except Exception as e:
                    print(f"  [{updated + skipped + 1}/{len(rows)}] {note_id} 失败: {e}", file=sys.stderr)
            print(f"[OK] 刷新完成: {updated} 条更新, {skipped} 条未变更")
            return 0
    finally:
        hb.stop()


def cmd_export(args: argparse.Namespace) -> int:
    conn = xhs_storage.connect()
    try:
        if args.format == "md":
            if not args.note:
                print("[ERR] --format md 需配合 --note <id>", file=sys.stderr)
                return 1
            row = xhs_storage.get_note(conn, args.note)
            if not row:
                print(f"[ERR] 笔记 {args.note} 不在 DB", file=sys.stderr)
                return 1
            path = xhs_storage.write_markdown(conn, args.note)
            title = row["title"] or "(无标题)"
            print(f"[OK] 《{title}》已导出 Markdown")
            print(f"     文件: {path}")
            summary = xhs_storage.render_update_summary(conn, args.note)
            if summary:
                print(f"     内容: {summary}")
        elif args.format == "json":
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            path = xhs_storage.write_json(conn)
            print(f"[OK] 已导出 JSON（共 {total} 条笔记）")
            print(f"     文件: {path}")
        elif args.format == "xlsx":
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            try:
                path = xhs_storage.write_xlsx(conn)
                print(f"[OK] 已导出 XLSX（共 {total} 条笔记）")
                print(f"     文件: {path}")
            except ImportError:
                return 1
        else:
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            path = xhs_storage.write_csv(conn)
            print(f"[OK] 已导出 CSV（共 {total} 条笔记）")
            print(f"     文件: {path}")
        return 0
    finally:
        conn.close()


def cmd_feed(args: argparse.Namespace) -> int:
    """推荐流 / 分类流浏览，每页入库并打印摘要。"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            category_key = getattr(args, "category", "recommend")
            category = xhs_config.FEED_CATEGORIES.get(category_key, "homefeed_recommend")
            total = 0
            cursor_score = ""
            for page in range(1, args.pages + 1):
                data = fetch_feed(fetcher, category=category, cursor_score=cursor_score, num=args.num)
                items = data.get("items") or []
                if not items:
                    print(f"  [page {page}] 空结果，结束", file=sys.stderr)
                    break
                for item in items:
                    if item.get("model_type") != "note":
                        continue
                    note = _normalize_note(item)
                    if not note["note_id"]:
                        continue
                    xhs_storage.upsert_note(conn, note)
                    _try_upsert_user_from_note(note, conn)
                    total += 1
                    tag = _type_tag(note)
                    preview = _desc_preview(note)
                    print(f"  [{total}] [{tag}] {note['note_id']} {preview}")
                conn.commit()
                cursor_score = data.get("cursor_score", "")
                if not data.get("has_more"):
                    break
            print(f"[OK] feed ({category_key}) 共入库 {total} 条")
            return 0
    finally:
        hb.stop()


def cmd_crawl_feed(args: argparse.Namespace) -> int:
    """长任务版 feed：cursor 落库 + --resume 续抓。"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            category_key = getattr(args, "category", "recommend")
            category = xhs_config.FEED_CATEGORIES.get(category_key, "homefeed_recommend")
            task_id = f"feed:{category_key}"
            cursor_score = ""
            start_page = 1
            if args.resume:
                st = xhs_storage.get_crawl_state(conn, task_id)
                if st:
                    cursor_score = st["cursor"] or ""
                    print(f"[RESUME] 从 cursor={cursor_score[:30]} 继续", file=sys.stderr)

            total = 0
            try:
                for page in range(start_page, args.max_pages + 1):
                    data = fetch_feed(fetcher, category=category, cursor_score=cursor_score)
                    items = data.get("items") or []
                    if not items:
                        print(f"  [page {page}] 空结果，结束", file=sys.stderr)
                        break
                    page_count = 0
                    for item in items:
                        if item.get("model_type") != "note":
                            continue
                        note = _normalize_note(item)
                        if not note["note_id"]:
                            continue
                        xhs_storage.upsert_note(conn, note)
                        _try_upsert_user_from_note(note, conn)
                        total += 1
                        page_count += 1
                        xhs_media.post_process_note(note, conn, args)
                    conn.commit()
                    cursor_score = data.get("cursor_score", "")
                    xhs_storage.update_crawl_state(
                        conn, task_id, "feed", category_key,
                        cursor_score, "running", "",
                    )
                    print(f"  [page {page}] +{page_count} 入库，累计 {total}", file=sys.stderr)
                    if not data.get("has_more") or not cursor_score:
                        print("  has_more=False 或 cursor 为空，结束", file=sys.stderr)
                        break
                xhs_storage.update_crawl_state(
                    conn, task_id, "feed", category_key,
                    cursor_score, "completed", "",
                )
                print(f"[OK] crawl-feed ({category_key}) 完成：{total} 条新增")
                return 0
            except FatalRiskError as e:
                xhs_storage.update_crawl_state(
                    conn, task_id, "feed", category_key,
                    cursor_score, "paused", str(e),
                )
                print(f"[PAUSED] 因风控暂停：{e}\n下次用 --resume 继续。", file=sys.stderr)
                return 2
    finally:
        hb.stop()


def cmd_download(args: argparse.Namespace) -> int:
    """下载某笔记的图片/视频到 data/media/<note_id>/。不需要签名（直接从 CDN 拉）。"""
    hb = _Heartbeat()
    try:
        conn = xhs_storage.connect()
        try:
            row = xhs_storage.get_note(conn, args.note_id)
            if not row:
                print(f"[ERR] 笔记 {args.note_id} 不在 DB", file=sys.stderr)
                return 1

            title = row["title"] or "(无标题)"
            with_video = getattr(args, "with_video", True)
            overwrite = getattr(args, "overwrite", False)
            n_ok, n_video, n_err, out = xhs_media.download_media(
                args.note_id, conn, with_video=with_video, overwrite=overwrite
            )

            rel = out.relative_to(xhs_config.MEDIA_DIR)
            print(f"[OK] 《{title}》媒体下载完成")
            parts = []
            if n_ok:
                parts.append(f"{n_ok} 张图片")
            if n_video:
                parts.append("1 个视频")
            if n_err:
                parts.append(f"{n_err} 个失败")
            if parts:
                print(f"     结果: {'、'.join(parts)}")
            print(f"     存放: media/{rel}")
            # 重新渲染 MD（会自动用本地路径）
            try:
                md = xhs_storage.write_markdown(conn, args.note_id)
                print(f"     MD 已更新（图片/视频改用本地路径）: {md.name}")
            except Exception:
                pass
            return 0 if n_err == 0 else 2
        finally:
            conn.close()
    finally:
        hb.stop()


def cmd_crawl_search(args: argparse.Namespace) -> int:
    """长任务版 search：多页 + cursor 落库 + --resume 续抓"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            task_id = f"search:{args.keyword}"
            # 恢复
            start_page = 1
            if args.resume:
                st = xhs_storage.get_crawl_state(conn, task_id)
                if st:
                    start_page = int(st["cursor"] or "1")
                    print(f"[RESUME] 从第 {start_page} 页继续（上次状态：{st['status']}）", file=sys.stderr)

            total = 0
            last_page = start_page - 1
            try:
                for page in range(start_page, args.max_pages + 1):
                    data = fetch_search(fetcher, args.keyword, page=page)
                    items = data.get("items") or []
                    if not items:
                        print(f"  [page {page}] 空结果，结束", file=sys.stderr)
                        break
                    note_ids: list[str] = []
                    for item in items:
                        if item.get("model_type") != "note":
                            continue
                        item.setdefault("xsec_source", "pc_search")
                        note = _normalize_note(item)
                        if not note["note_id"]:
                            continue
                        xhs_storage.upsert_note(conn, note)
                        _try_upsert_user_from_note(note, conn)
                        note_ids.append(note["note_id"])
                        total += 1
                        xhs_media.post_process_note(note, conn, args)
                    xhs_storage.save_search_page(conn, args.keyword, page, note_ids)
                    conn.commit()
                    last_page = page
                    xhs_storage.update_crawl_state(
                        conn, task_id, "search", args.keyword,
                        str(page + 1), "running", "",
                    )
                    print(f"  [page {page}] +{len(note_ids)} 入库，累计 {total}", file=sys.stderr)
                    if not data.get("has_more"):
                        print("  has_more=False，结束", file=sys.stderr)
                        break
                # cursor 保持 last_page + 1（下次 --resume 从这开始）
                xhs_storage.update_crawl_state(
                    conn, task_id, "search", args.keyword,
                    str(last_page + 1), "completed", "",
                )
                print(f"[OK] crawl-search '{args.keyword}' 完成：{total} 条新增（下次 --resume 从 page {last_page + 1}）")
                return 0
            except FatalRiskError as e:
                paused_page = page if 'page' in dir() else last_page + 1
                xhs_storage.update_crawl_state(
                    conn, task_id, "search", args.keyword,
                    str(max(paused_page, last_page + 1)), "paused", str(e),
                )
                print(f"[PAUSED] 在第 {paused_page} 页因风控暂停：{e}\n下次用 --resume 继续。", file=sys.stderr)
                return 2
    finally:
        hb.stop()


def cmd_crawl_user(args: argparse.Namespace) -> int:
    """长任务版 user：cursor 落库 + --resume"""
    hb = _Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            task_id = f"user:{args.user_id}"
        cursor = ""
        if args.resume:
            st = xhs_storage.get_crawl_state(conn, task_id)
            if st:
                cursor = st["cursor"] or ""
                print(f"[RESUME] 从 cursor={cursor[:30]} 继续", file=sys.stderr)

        # 先入库用户信息
        try:
            info = fetch_user_info(fetcher, args.user_id)
            if info:
                info["user_id"] = args.user_id
                xhs_storage.upsert_user(conn, _normalize_user(info))
                conn.commit()
                print(f"[USER] {info.get('nickname','?')} 粉丝 {info.get('fans','?')}", file=sys.stderr)
        except Exception as e:
            print(f"[USER] 用户信息抓取失败（继续抓笔记）：{e}", file=sys.stderr)

        total = 0
        try:
            for page in range(1, args.max_pages + 1):
                data = fetch_user_notes(fetcher, args.user_id, cursor=cursor)
                items = data.get("notes") or []
                if not items:
                    break
                for item in items:
                    item.setdefault("xsec_source", "pc_user")
                    note = _normalize_note({
                        "id": item.get("note_id") or item.get("id"),
                        "xsec_token": item.get("xsec_token", ""),
                        "xsec_source": "pc_user",
                        "note_card": item,
                    })
                    if not note["note_id"]:
                        continue
                    note["user_id"] = args.user_id
                    xhs_storage.upsert_note(conn, note)
                    _try_upsert_user_from_note(note, conn)
                    total += 1
                    xhs_media.post_process_note(note, conn, args)
                conn.commit()
                cursor = data.get("cursor", "")
                xhs_storage.update_crawl_state(
                    conn, task_id, "user", args.user_id,
                    cursor, "running", "",
                )
                print(f"  [page {page}] +{len(items)} 入库，累计 {total}", file=sys.stderr)
                if not data.get("has_more") or not cursor:
                    break
            xhs_storage.update_crawl_state(
                conn, task_id, "user", args.user_id, cursor, "completed", "",
            )
            print(f"[OK] crawl-user {args.user_id} 完成：{total} 条新增")
            return 0
        except FatalRiskError as e:
            xhs_storage.update_crawl_state(
                conn, task_id, "user", args.user_id, cursor, "paused", str(e),
            )
            print(f"[PAUSED] cursor={cursor[:30]} 因风控暂停：{e}\n下次用 --resume 继续。", file=sys.stderr)
            return 2
    finally:
        hb.stop()


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sign-mode", choices=["auto", "embed-js", "playwright", "py-port"], default="auto")
    p.add_argument("--speed-mode", choices=list(xhs_config.SPEED_PROFILES.keys()), default="normal")
    p.add_argument("--proxy", default=None)
    p.add_argument("--account", default=None, help="指定账号别名")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xhs", description="小红书爬虫 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="安装所有依赖（首次运行自动触发，此命令用于排查）")
    p_setup.set_defaults(func=cmd_setup)

    p_health = sub.add_parser("health", help="系统健康检查（依赖+签名+账号+DB）")
    p_health.set_defaults(func=cmd_health)

    p_login = sub.add_parser("login", help="获取并保存 cookie")
    p_login.add_argument("--prefer",
                          choices=["auto", "win-edge", "win-chrome", "rookie",
                                   "wsl-edge", "wsl-edge-cdp",
                                   "wsl-chrome", "wsl-chrome-cdp", "qr", "manual"],
                          default="auto")
    p_login.add_argument("--name", default=None, help="账号别名（保存到 data/accounts/<name>.json）")
    p_login.set_defaults(func=cmd_login)

    p_sign = sub.add_parser("sign-test", help="三档签名健康检查")
    p_sign.set_defaults(func=cmd_sign_test)

    p_note = sub.add_parser("note", help="抓单篇笔记详情")
    p_note.add_argument("note_id")
    p_note.add_argument("--xsec-token", default="")
    _add_common(p_note)
    p_note.set_defaults(func=cmd_note)

    p_user = sub.add_parser("user", help="抓用户主页 + 笔记列表前 N 页")
    p_user.add_argument("user_id")
    p_user.add_argument("--pages", type=int, default=3)
    p_user.add_argument("--download", action="store_true", help="每条笔记入库后自动下载媒体")
    p_user.add_argument("--analyze", action="store_true", help="视频笔记自动做视频分析（需 ffmpeg）")
    _add_common(p_user)
    p_user.set_defaults(func=cmd_user)

    p_search = sub.add_parser("search", help="关键词搜索前 N 页")
    p_search.add_argument("keyword")
    p_search.add_argument("--pages", type=int, default=2)
    p_search.add_argument("--download", action="store_true", help="每条笔记入库后自动下载媒体")
    p_search.add_argument("--analyze", action="store_true", help="视频笔记自动做视频分析（需 ffmpeg）")
    _add_common(p_search)
    p_search.set_defaults(func=cmd_search)

    p_com = sub.add_parser("comments", help="抓某笔记的评论树（含子评论分页）")
    p_com.add_argument("note_id")
    p_com.add_argument("--max-pages", type=int, default=10, help="主评论最多抓几页（每页约20条）")
    p_com.add_argument("--max-sub-pages", type=int, default=5, help="每条主评论最多抓几页子评论")
    p_com.add_argument("--no-sub", dest="with_sub", action="store_false", help="不抓子评论分页（只要内联的）")
    _add_common(p_com)
    p_com.set_defaults(func=cmd_comments, with_sub=True)

    p_dl = sub.add_parser("download", help="下载某笔记的图片/视频到 data/media/<note_id>/")
    p_dl.add_argument("note_id")
    p_dl.add_argument("--no-video", dest="with_video", action="store_false")
    p_dl.add_argument("--overwrite", action="store_true", help="已下载也重下")
    p_dl.set_defaults(func=cmd_download, with_video=True)

    p_cs = sub.add_parser("crawl-search", help="长任务：关键词多页 + 断点续抓")
    p_cs.add_argument("keyword")
    p_cs.add_argument("--max-pages", type=int, default=20)
    p_cs.add_argument("--resume", action="store_true", help="从 crawl_state 里的 cursor 继续")
    p_cs.add_argument("--download", action="store_true", default=True, help="自动下载媒体（默认开启）")
    p_cs.add_argument("--no-download", dest="download", action="store_false", help="不下载媒体")
    p_cs.add_argument("--analyze", action="store_true", default=True, help="自动内容分析（默认开启）")
    p_cs.add_argument("--no-analyze", dest="analyze", action="store_false", help="不分析内容")
    _add_common(p_cs)
    p_cs.set_defaults(func=cmd_crawl_search)

    p_cu = sub.add_parser("crawl-user", help="长任务：某用户全部笔记 + 断点续抓")
    p_cu.add_argument("user_id")
    p_cu.add_argument("--max-pages", type=int, default=50)
    p_cu.add_argument("--resume", action="store_true")
    p_cu.add_argument("--download", action="store_true", default=True, help="自动下载媒体（默认开启）")
    p_cu.add_argument("--no-download", dest="download", action="store_false", help="不下载媒体")
    p_cu.add_argument("--analyze", action="store_true", default=True, help="自动内容分析（默认开启）")
    p_cu.add_argument("--no-analyze", dest="analyze", action="store_false", help="不分析内容")
    _add_common(p_cu)
    p_cu.set_defaults(func=cmd_crawl_user)

    p_clean = sub.add_parser("cleanup", help="数据清理：孤儿媒体、过期缓存")
    p_clean.add_argument("--dry-run", action="store_true", help="只显示要删除的内容")
    p_clean.add_argument("--max-cache-days", type=int, default=30, help="清理 N 天前的空搜索缓存（默认 30）")
    p_clean.add_argument("--max-state-days", type=int, default=7, help="清理 N 天前的已完成抓取状态（默认 7）")
    p_clean.add_argument("--vacuum", action="store_true", help="SQLite VACUUM 压缩数据库")
    p_clean.set_defaults(func=cmd_cleanup)

    p_ref = sub.add_parser("refresh", help="重抓超过 N 小时的旧笔记（增量更新）")
    p_ref.add_argument("--max-age-hours", type=int, default=24, help="重抓 N 小时前的笔记（默认 24）")
    p_ref.add_argument("--limit", type=int, default=100, help="最多刷新几条（默认 100）")
    _add_common(p_ref)
    p_ref.set_defaults(func=cmd_refresh)

    p_exp = sub.add_parser("export", help="从 DB 导出 MD/CSV/JSON/XLSX")
    p_exp.add_argument("--format", choices=["md", "csv", "json", "xlsx"], default="csv")
    p_exp.add_argument("--note", default=None, help="单篇 MD 时指定 note_id")
    p_exp.set_defaults(func=cmd_export)

    p_acct = sub.add_parser("accounts", help="查看多账号状态")
    p_acct.set_defaults(func=cmd_accounts)

    p_stats = sub.add_parser("stats", help="请求统计")
    p_stats.add_argument("--hours", type=int, default=None)
    p_stats.add_argument("--account", default=None)
    p_stats.set_defaults(func=cmd_stats)

    p_ujs = sub.add_parser("update-js", help="从 cv-cat/Spider_XHS 拉取最新签名 JS")
    p_ujs.add_argument("--dry-run", action="store_true", help="只检查不覆盖")
    p_ujs.set_defaults(func=cmd_update_js)

    p_feed = sub.add_parser("feed", help="推荐流 / 分类流浏览")
    p_feed.add_argument("--category", choices=list(xhs_config.FEED_CATEGORIES.keys()), default="recommend")
    p_feed.add_argument("--pages", type=int, default=3)
    p_feed.add_argument("--num", type=int, default=18, help="每页条数")
    _add_common(p_feed)
    p_feed.set_defaults(func=cmd_feed)

    p_cf = sub.add_parser("crawl-feed", help="长任务：推荐流多页 + 断点续抓")
    p_cf.add_argument("--category", choices=list(xhs_config.FEED_CATEGORIES.keys()), default="recommend")
    p_cf.add_argument("--max-pages", type=int, default=20)
    p_cf.add_argument("--resume", action="store_true")
    p_cf.add_argument("--download", action="store_true", default=True, help="自动下载媒体（默认开启）")
    p_cf.add_argument("--no-download", dest="download", action="store_false", help="不下载媒体")
    p_cf.add_argument("--analyze", action="store_true", default=True, help="自动内容分析（默认开启）")
    p_cf.add_argument("--no-analyze", dest="analyze", action="store_false", help="不分析内容")
    _add_common(p_cf)
    p_cf.set_defaults(func=cmd_crawl_feed)

    p_rc = sub.add_parser("refresh-cookies", help="批量检查并刷新所有账号的 cookie")
    p_rc.add_argument("--force", action="store_true", help="强制重新登录（即使 cookie 未过期）")
    p_rc.set_defaults(func=cmd_refresh_cookies)

    p_az = sub.add_parser("analyze", help="评论情感分析 / 话题聚类")
    p_az.add_argument("--type", choices=["sentiment", "topics"], default="topics",
                      help="分析类型（默认 topics）")
    p_az.add_argument("--note", default=None, help="限定单篇笔记（情感分析）")
    p_az.add_argument("--keyword", default=None, help="限定搜索关键词")
    p_az.add_argument("--user", default=None, help="限定用户 ID（话题聚类）")
    p_az.add_argument("--output", choices=["text", "json"], default="text", help="输出格式")
    p_az.set_defaults(func=cmd_analyze)

    p_av = sub.add_parser("analyze-video", help="视频内容智能分析（语音转文字 + OCR + AI 摘要）")
    p_av.add_argument("note_id", help="视频笔记 ID（需已入库）")
    p_av.add_argument("--mode", choices=["none", "local", "ollama", "openai", "mcp"], default=None,
                      help="AI 摘要模式（覆盖配置文件）")
    p_av.add_argument("--whisper-model", default=None, help="Whisper 模型（tiny/base/small/medium/large-v3）")
    p_av.add_argument("--frame-interval", type=int, default=None, help="关键帧间隔秒数")
    p_av.add_argument("--max-duration", type=int, default=None,
                      help="最多转录前 N 秒音频（默认 300，0=不限制）")
    p_av.add_argument("--step", nargs="*", default=None,
                      help="分段执行: extract transcribe ocr summary（不传=全部执行）")
    _add_common(p_av)
    p_av.set_defaults(func=cmd_analyze_video)

    p_sv = sub.add_parser("setup-video", help="交互式配置视频分析")
    p_sv.add_argument("--mode", choices=["none", "local", "ollama", "openai", "mcp"], default=None,
                      help="直接指定 AI 摘要模式（跳过交互）")
    p_sv.add_argument("--whisper-model", default=None, help="直接指定 Whisper 模型")
    p_sv.add_argument("--frame-interval", type=int, default=None, help="关键帧间隔秒数")
    p_sv.set_defaults(func=cmd_setup_video)

    p_ai = sub.add_parser("analyze-images", help="图片内容智能分析（OCR + AI 视觉 + Mermaid）")
    p_ai.add_argument("note_id", help="图文笔记 ID（需已入库）")
    p_ai.add_argument("--mode", choices=["auto", "none", "local", "vision"], default=None,
                      help="分析模式（覆盖配置文件）")
    p_ai.add_argument("--backend", choices=["ollama", "api", "mcp"], default=None,
                      help="视觉后端（覆盖配置文件）")
    p_ai.add_argument("--no-mermaid", action="store_true", help="关闭 Mermaid 图表生成")
    p_ai.add_argument("--step", nargs="*", default=None,
                      help="分段执行: ocr vision mermaid（不传=全部执行）。"
                           "适合终端 timeout 60s 的环境")
    _add_common(p_ai)
    p_ai.set_defaults(func=cmd_analyze_images)

    p_si = sub.add_parser("setup-image", help="交互式配置图片分析")
    p_si.add_argument("--mode", choices=["auto", "none", "local", "vision"], default=None,
                      help="直接指定分析模式（跳过交互）")
    p_si.add_argument("--backend", choices=["ollama", "api", "mcp"], default=None,
                      help="直接指定视觉后端（跳过交互）")
    p_si.add_argument("--no-mermaid", action="store_true", help="关闭 Mermaid 图表")
    p_si.set_defaults(func=cmd_setup_image)

    p_sw = sub.add_parser("setup-wizard", help="统一引导向导：配置图片+视频分析")
    p_sw.set_defaults(func=cmd_setup_wizard)

    return p


def main(argv: list[str] | None = None) -> int:
    # Fix Windows console encoding for Unicode output
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 注册 SIGTERM：确保 PID 文件被清理
    import signal
    def _sigterm_handler(sig, frame):
        xhs_storage._release_lock()
        sys.exit(143)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # 首次运行自动安装依赖（延迟到 main() 而非模块级，避免 import 副作用）
    xhs_bootstrap.ensure_ready()

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FatalRiskError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        xhs_storage._release_lock()
        print("\n[ABORT] 用户中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[ERR] {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
