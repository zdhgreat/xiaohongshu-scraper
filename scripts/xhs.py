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

__version__ = "3.0.0"

import argparse
import io
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import xhs_accounts
import xhs_bootstrap
import xhs_config
from xhs_config import Heartbeat
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
from xhs_image import cmd_analyze_images
from xhs_video import cmd_analyze_video, cmd_setup_video



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
    # 搜索/推荐流 API 返回格式中用户数据嵌套在 note_card.user 下
    if not user_info:
        user_info = (raw.get("note_card") or {}).get("user") or {}
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


def _try_pg_sync() -> None:
    """命令成功后自动尝试同步 SQLite → PG（POSTGRES_DB 配置的库）。

    静默失败：PG 不可用、hub_adapter 未安装等情况不影响主流程。
    """
    # 只对会写入数据的命令做同步（login/health/stats 等跳过）
    try:
        adapter_path = Path(__file__).resolve().parent.parent / "hub_adapter.py"
        if not adapter_path.exists():
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_adapter", str(adapter_path))
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        pg_conn = mod.get_pg_connection()
        try:
            # 先建表/迁移列：链路 B（xhs.py 自动同步）不经 hub_adapter.main()，
            # 若 PG 库是旧 schema（缺 status/media_path 列），upsert 会静默失败
            mod.init_schema(pg_conn)
            count = mod.sync_sqlite_to_pg(pg_conn)
            if count:
                print(f"[PG-SYNC] 已自动同步到 PG ({os.getenv('POSTGRES_DB', 'financial_hub')})")
        finally:
            pg_conn.close()
    except ImportError:
        pass  # psycopg2 / financial_hub_postgres 未安装
    except Exception:
        pass  # PG 不可用，不影响主流程


def _print_correction_hint() -> None:
    """数据写入后检测待纠错视频转录，打印醒目提示，引导执行 correct。"""
    try:
        conn = xhs_storage.connect()
        rows = xhs_storage.list_pending_corrections(conn, limit=9999)
        conn.close()
    except Exception:
        return
    if rows:
        print(f"\n⚠️ [CORRECT] 检测到 {len(rows)} 条视频转录待纠错"
              f"（Whisper 原始输出含同音字/英文错误，不可直接使用）。")
        print(f"   列出：python scripts/xhs.py correct --list")
        print(f"   写回：python scripts/xhs.py correct --note <id> --apply \"<纠错后转录>\"")


# ---------------------------------------------------------------------------
# Fetcher 工厂
# ---------------------------------------------------------------------------

def _validate_accounts(mgr: xhs_accounts.AccountManager) -> None:
    """启动预检：在线验证所有账号 cookie，无效的尝试自动恢复。"""
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
        if valid is None:
            print(f"  [{alias:15s}] 网络异常（不跳过）", file=sys.stderr)
            continue
        if valid:
            acc.cookies = updated_cookies
            acc.save_cookies()
            nickname = (user_info or {}).get("nickname", "")
            print(f"  [{alias:15s}] 有效{f' ({nickname})' if nickname else ''}", file=sys.stderr)
        else:
            # 尝试自动恢复（Profile Session 恢复 → win-native）
            import xhs_keepalive
            success, status = xhs_keepalive.keepalive_single_account(alias, acc)
            if not success:
                acc.mark_invalid()
                print(f"  [{alias:15s}] 自动恢复失败（已标记冷却）", file=sys.stderr)
            else:
                print(f"  [{alias:15s}] 自动恢复成功: {status}", file=sys.stderr)
    mgr.save_state()


def _make_fetcher(args: argparse.Namespace) -> Fetcher:
    # 1. 账号
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("[CLI] 未找到任何账号，开始登录到 default...", file=sys.stderr)
        cookies, cookie_meta = xhs_login.acquire_cookies(prefer="auto")
        xhs_login.persist_cookies(cookies, cookie_meta)
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
    # 3. signer / speed（统一 paranoid 速度）
    acc = mgr.get(force)
    signer = xhs_sign.make_signer(args.sign_mode)
    speed = xhs_config.SPEED_PROFILES["paranoid"]
    return Fetcher(signer, speed, mgr, proxy_pool,
                    force_account=force,
                    sign_mode_label=args.sign_mode,
                    speed_mode_label="paranoid")


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
    cookies, cookie_meta = xhs_login.acquire_cookies(prefer=args.prefer, headless_qr=False, profile_hint=name)
    if args.name and args.name != "default":
        # 多账号：落到 data/accounts/<name>.json
        xhs_config.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        path = xhs_config.ACCOUNTS_DIR / f"{args.name}.json"
        data = {"_version": 2, "cookies": cookies, "cookie_meta": cookie_meta}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        xhs_config.restrict_file(path)
        print(f"[OK] 已保存 {len(cookies)} 个 cookie 到 {path}（账号别名: {args.name}）")
    else:
        xhs_login.persist_cookies(cookies, cookie_meta)
        print(f"[OK] 已保存 {len(cookies)} 个 cookie 到 {xhs_config.COOKIES_PATH}")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("无账号。先跑 `login` 或 `login --name <alias>`。")
        return 0
    # --set-speed 已废弃：现在只有 paranoid 一档
    set_speed = getattr(args, 'set_speed', None)
    if set_speed:
        print("[INFO] 速度模式已统一为 paranoid，--set-speed 不再需要", file=sys.stderr)
    print(f"=== 共 {len(mgr.accounts)} 个账号 ===")
    for s in mgr.stats():
        cd = f"cooldown→{s['cooldown_until']}" if s['cooldown_until'] else ""
        acc = mgr.accounts[s['alias']]
        print(f"  [{s['alias']:15s}] 日抓 {s['daily_count']:3d}/{xhs_config.DAILY_HARD_CAP}  累计 {s['total_calls']:5d}"
              f"  460×{s['last_460']}  461×{s['last_461']}  最近用 {s['last_used'] or '从未'}  {cd}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    s = xhs_log.stats(hours=args.hours, account=args.account)
    xhs_log.print_stats(s)
    return 0


def cmd_sign_test(args: argparse.Namespace) -> int:
    loaded = xhs_login.load_cookies()
    cookies = loaded[0] if loaded else {}
    a1 = cookies.get("a1")
    results = xhs_sign.run_sign_test(a1=a1)
    ok_any = any(results.values())
    return 0 if ok_any else 1


def cmd_update_js(args: argparse.Namespace) -> int:
    import xhs_update_js
    return xhs_update_js.run_update_js(dry_run=getattr(args, "dry_run", False))


def cmd_update_fp(args: argparse.Namespace) -> int:
    """全量更新 UA/指纹池/TLS/签名JS。"""
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from updater import run
    return 0 if run(dry_run=getattr(args, "dry_run", False)) else 1


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
        new_cookies, new_meta = xhs_login.acquire_cookies(prefer="qr", profile_hint=alias)
        acc.cookies = new_cookies
        acc.cookie_meta = new_meta
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


def cmd_keepalive(args: argparse.Namespace) -> int:
    """多账号 Cookie 自动保活。"""
    import xhs_keepalive
    mgr = xhs_accounts.AccountManager()
    if not mgr.has_accounts():
        print("无账号。先跑 `login` 或 `login --name <alias>`。")
        return 0
    return xhs_keepalive.run_daemon(
        mgr,
        interval_s=getattr(args, 'interval', 0),
        single_run=not getattr(args, 'daemon', False),
        force=getattr(args, 'force', False),
        account=getattr(args, 'account', None),
    )




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
            print("[NOTE] ⚠️ 无 xsec_token，直访 detail 极易触发 461。尝试搜索获取 token…",
                  file=sys.stderr)
            # 自动降级：通过搜索发现该笔记，获取 xsec_token
            try:
                search_results = fetch_search(fetcher, args.note_id, page=1, page_size=5)
                items = search_results.get("items") or search_results.get("data", {}).get("items") or []
                for it in items:
                    nid = it.get("id") or (it.get("note_card") or {}).get("note_id", "")
                    if nid == args.note_id:
                        xsec_token = it.get("xsec_token", "")
                        xsec_source = it.get("xsec_source", "pc_search")
                        if xsec_token:
                            print(f"[NOTE] 通过搜索获取到 xsec_token（来源 {xsec_source}）",
                                  file=sys.stderr)
                            # 顺便存入 DB 供下次使用
                            break
            except Exception as e:
                print(f"[NOTE] 搜索降级失败: {e}", file=sys.stderr)

            if not xsec_token:
                print("[NOTE] ❌ 无法获取 xsec_token（搜索未命中且 DB 无记录），停止请求以避免 461 风控。",
                      file=sys.stderr)
                print("[NOTE] 建议：先运行 search 或 user 命令让笔记入库（自带 token），再执行 note 命令。",
                      file=sys.stderr)
                return 1

        item = fetch_note_detail(fetcher, args.note_id, xsec_token=xsec_token, xsec_source=xsec_source)
        if not item:
            print(f"[NOTE] 笔记 {args.note_id} 不存在或已被删除", file=sys.stderr)
            return
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
        print(f"     文件: {path.parent.name}/{path.name}")
        # 自动下载图片/视频
        xhs_media.auto_download_note(note, conn)
        # 视频笔记自动分析（下载→转录→OCR→纠错）
        if note.get("type") == "video":
            try:
                video_local = xhs_media.find_video_local(note["note_id"], conn)
                if not video_local:
                    # note detail API 返回完整数据，upsert_note 已写入 DB
                    xhs_media.download_media(note["note_id"], conn, with_video=True)
                    video_local = xhs_media.find_video_local(note["note_id"], conn)
                if video_local and video_local.exists():
                    import xhs_video
                    cfg = xhs_video.load_config()
                    result = xhs_video.analyze_video(video_local, cfg)
                    ocr_text = json.dumps(result.get("ocr_results", []), ensure_ascii=False)
                    xhs_storage.update_video_analysis(
                        conn, note["note_id"],
                        transcript=result.get("transcript", ""),
                        ocr_text=ocr_text,
                        summary="llm纠错" if result.get("corrected") else "",
                    )
                    parts = []
                    if result.get("transcript"):
                        parts.append(f"转录{len(result['transcript'])}字")
                    if result.get("corrected"):
                        parts.append("已纠错")
                    if result.get("ocr_results"):
                        parts.append(f"OCR {len(result['ocr_results'])}帧")
                    if parts:
                        print(f"     视频分析: {'、'.join(parts)}")
                    xhs_storage.write_markdown(conn, note["note_id"])
            except Exception as e:
                print(f"     [WARN] 视频分析跳过: {e}", file=sys.stderr)
        summary = xhs_storage.render_update_summary(conn, note["note_id"])
        if summary:
            print(f"     状态: {summary}")
        return 0


def cmd_user(args: argparse.Namespace) -> int:
    hb = Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            # 昵称 → user_id
            resolved = xhs_storage.resolve_user_id(conn, args.user_id)
            if resolved:
                if resolved != args.user_id:
                    print(f"[USER] 昵称「{args.user_id}」→ {resolved}")
                args.user_id = resolved
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
                    note_id = item.get("note_id") or item.get("id")
                    xsec_token = item.get("xsec_token", "")
                    # 列表接口数据不完整，逐条调详情接口补全
                    try:
                        detail = fetch_note_detail(fetcher, note_id, xsec_token=xsec_token, xsec_source="pc_user")
                        if not detail:
                            raise ValueError("笔记不存在")
                        note_card = detail.get("note_card") or detail
                        note = _normalize_note(note_card)
                    except Exception:
                        # 详情失败则用列表数据兜底
                        note = _normalize_note({
                            "id": note_id,
                            "xsec_token": xsec_token,
                            "xsec_source": "pc_user",
                            "note_card": item,
                        })
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
    hb = Heartbeat()
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
    hb = Heartbeat()
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
            print(f"     文件: {path.parent.name}/{path.name}")
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
                    "DELETE FROM search_cache WHERE note_ids_json = '' OR note_ids_json = '[]'"
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


def cmd_correct(args: argparse.Namespace) -> int:
    """Agent 手动纠错视频转录：列出待纠错 / 读取单条 / 写回纠错结果。

    用法:
      correct --list [--limit N]              列出待纠错视频笔记
      correct --note <id>                     打印单条 transcript + ocr（供纠错参照）
      correct --note <id> --apply "<纠错后>"   写回 video_transcript，标记 video_summary='agent纠错'
    """
    conn = xhs_storage.connect()
    try:
        if getattr(args, "list", False):
            limit = getattr(args, "limit", 50) or 50
            rows = xhs_storage.list_pending_corrections(conn, limit=limit)
            if not rows:
                print("[CORRECT] 没有待纠错的视频笔记")
                return 0
            for r in rows:
                print(f"--- {r['note_id']} | {r['title'] or '(无标题)'} ---")
                print(f"[TRANSCRIPT]\n{r['video_transcript'] or ''}")
                print(f"[OCR]\n{r['video_ocr_text'] or ''}")
            print(f"\n[CORRECT] 共 {len(rows)} 条待纠错。"
                  f"纠错后用：xhs.py correct --note <id> --apply \"<纠错后转录>\"")
            return 0

        note_id = getattr(args, "note", None)
        if not note_id:
            if getattr(args, "apply", None) is not None:
                print("[ERROR] --apply 必须配合 --note <id> 使用", file=sys.stderr)
            else:
                print('用法: correct --list | --note <id> [--apply "<纠错后转录>"]',
                      file=sys.stderr)
            return 1

        note = xhs_storage.get_note(conn, note_id)
        if not note:
            print(f"[ERROR] 笔记不存在: {note_id}", file=sys.stderr)
            return 1

        apply_text = getattr(args, "apply", None)
        if apply_text is None:
            # 只读：打印单条 transcript + ocr
            print(f"--- {note['note_id']} | {note['title'] or '(无标题)'} ---")
            print(f"[TRANSCRIPT]\n{note['video_transcript'] or ''}")
            print(f"[OCR]\n{note['video_ocr_text'] or ''}")
            return 0

        # 写回：ocr_text 原样回填，避免 update_video_analysis 三参数覆盖清空 ocr
        xhs_storage.update_video_analysis(
            conn, note_id,
            transcript=apply_text,
            ocr_text=note["video_ocr_text"] or "",
            summary="agent纠错",
        )
        print(f"[OK] 已写回纠错转录: {note_id} (video_summary=agent纠错)")
        _try_pg_sync()  # 纠错是数据变更，写回后同步到 PG
        _print_correction_hint()  # 提示剩余待纠错（让 Agent 知道是否还有未处理项）
        return 0
    finally:
        conn.close()


def cmd_refresh(args: argparse.Namespace) -> int:
    """重抓超过 N 小时的笔记（增量更新）。"""
    hb = Heartbeat()
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
                    if not item:
                        skipped += 1
                        continue
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


def cmd_enrich(args: argparse.Namespace) -> int:
    """补全搜索入库的半成品笔记（无标题/无详情数据的笔记）。

    对通过 search 入库但未调详情 API 的笔记，逐条补全标题、描述、媒体等。
    """
    with fetch_session(args) as (fetcher, conn):
        # 找出"半成品"笔记：标题为空 或 raw_json 很短（搜索摘要）
        rows = conn.execute(
            "SELECT note_id, xsec_token, xsec_source, raw_json FROM notes "
            "WHERE (title = '' OR title IS NULL OR length(raw_json) < 200) "
            "AND xsec_token IS NOT NULL AND xsec_token != '' "
            "ORDER BY crawled_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        if not rows:
            print("[OK] 没有需要补全的半成品笔记")
            return 0
        print(f"[ENRICH] 找到 {len(rows)} 条半成品笔记需要补全", file=sys.stderr)

        enriched = 0
        failed = 0
        for i, row in enumerate(rows, 1):
            note_id = row["note_id"]
            xsec_token = row["xsec_token"]
            xsec_source = row["xsec_source"] or "pc_search"
            try:
                item = fetch_note_detail(fetcher, note_id,
                                          xsec_token=xsec_token,
                                          xsec_source=xsec_source)
                if not item:
                    failed += 1
                    print(f"  [{i}/{len(rows)}] {note_id} 不存在或已删除")
                    continue
                note = _normalize_note(item)
                note["note_id"] = note_id
                if not note.get("xsec_token"):
                    note["xsec_token"] = xsec_token
                    note["xsec_source"] = xsec_source
                xhs_storage.upsert_note(conn, note)
                if note["user_id"]:
                    user = (item.get("note_card") or {}).get("user") or {}
                    user["user_id"] = note["user_id"]
                    xhs_storage.upsert_user(conn, _normalize_user(user))
                conn.commit()
                enriched += 1
                title = note.get("title", "")[:40] or "(无标题)"
                print(f"  [{i}/{len(rows)}] {note_id} 《{title}》")
            except (FatalRiskError, KeyboardInterrupt):
                raise
            except Exception as e:
                failed += 1
                print(f"  [{i}/{len(rows)}] {note_id} 失败: {e}", file=sys.stderr)

        print(f"[ENRICH] 完成: {enriched} 条补全, {failed} 条失败", file=sys.stderr)
        print(f'__CRAWL_METRICS__:{{"items_found":{len(rows)},"items_new":{enriched},"items_failed":{failed}}}')
        return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn = xhs_storage.connect()
    user_id = getattr(args, "user", None)
    try:
        if args.format == "md":
            if args.note:
                # 单篇导出
                row = xhs_storage.get_note(conn, args.note)
                if not row:
                    print(f"[ERR] 笔记 {args.note} 不在 DB", file=sys.stderr)
                    return 1
                files = xhs_storage.write_markdown_files(conn, args.note)
                title = row["title"] or "(无标题)"
                print(f"[OK] 《{title}》已导出 Markdown")
                for f in files:
                    print(f"     {f.parent.name}/{f.name}")
                summary = xhs_storage.render_update_summary(conn, args.note)
                if summary:
                    print(f"     内容: {summary}")
            elif user_id:
                # 按博主批量导出
                rows = conn.execute(
                    "SELECT note_id FROM notes WHERE user_id = ? ORDER BY rowid",
                    (user_id,)
                ).fetchall()
                if not rows:
                    print(f"[ERR] 用户 {user_id} 没有笔记", file=sys.stderr)
                    return 1
                count = 0
                for (nid,) in rows:
                    try:
                        xhs_storage.write_markdown_files(conn, nid)
                        count += 1
                    except Exception:
                        pass
                print(f"[OK] 已导出 {count}/{len(rows)} 篇 Markdown（用户 {user_id}）")
            else:
                print("[ERR] --format md 需配合 --note <id> 或 --user <user_id>", file=sys.stderr)
                return 1
        elif args.format == "json":
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            path = xhs_storage.write_json(conn, user_id=user_id)
            label = f"（用户 {user_id}）" if user_id else f"（共 {total} 条笔记）"
            print(f"[OK] 已导出 JSON{label}")
            print(f"     文件: {path}")
        elif args.format == "xlsx":
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            try:
                path = xhs_storage.write_xlsx(conn, user_id=user_id)
                label = f"（用户 {user_id}）" if user_id else f"（共 {total} 条笔记）"
                print(f"[OK] 已导出 XLSX{label}")
                print(f"     文件: {path}")
            except ImportError:
                return 1
        else:
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            files = xhs_storage.write_csv(conn, user_id=user_id)
            label = f"（用户 {user_id}）" if user_id else f"（共 {total} 条笔记）"
            print(f"[OK] 已导出 CSV{label}")
            for f in files:
                print(f"     {f}")
        return 0
    finally:
        conn.close()


def cmd_feed(args: argparse.Namespace) -> int:
    """推荐流 / 分类流浏览，每页入库并打印摘要。"""
    hb = Heartbeat()
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
    hb = Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            category_key = getattr(args, "category", "recommend")
            category = xhs_config.FEED_CATEGORIES.get(category_key, "homefeed_recommend")
            task_id = f"feed:{category_key}:{fetcher.account.alias}"
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
                print(f'__CRAWL_METRICS__:{{"items_found":{total},"items_new":{total},"items_failed":0}}')
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
    hb = Heartbeat()
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
                print(f"     MD 已更新（图片/视频改用本地路径）: {md.parent.name}/{md.name}")
            except Exception:
                pass
            return 0 if n_err == 0 else 2
        finally:
            conn.close()
    finally:
        hb.stop()


def cmd_crawl_search(args: argparse.Namespace) -> int:
    """长任务版 search：多页 + cursor 落库 + --resume 续抓"""
    hb = Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            task_id = f"search:{args.keyword}:{fetcher.account.alias}"
            # 恢复
            start_page = 1
            if args.resume:
                st = xhs_storage.get_crawl_state(conn, task_id)
                if st:
                    start_page = int(st["cursor"] or "1")
                    print(f"[RESUME] 从第 {start_page} 页继续（上次状态：{st['status']}）", file=sys.stderr)

            total = 0
            last_page = start_page - 1
            page = start_page
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
                        str(page), "running", "",
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
                print(f'__CRAWL_METRICS__:{{"items_found":{total},"items_new":{total},"items_failed":0}}')
                return 0
            except FatalRiskError as e:
                paused_page = page
                xhs_storage.update_crawl_state(
                    conn, task_id, "search", args.keyword,
                    str(paused_page), "paused", str(e),
                )
                print(f"[PAUSED] 在第 {paused_page} 页因风控暂停：{e}\n下次用 --resume 继续。", file=sys.stderr)
                return 2
    finally:
        hb.stop()


def cmd_crawl_user(args: argparse.Namespace) -> int:
    """长任务版 user：cursor 落库 + --resume"""
    hb = Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            # 昵称 → user_id
            resolved = xhs_storage.resolve_user_id(conn, args.user_id)
            if resolved:
                if resolved != args.user_id:
                    print(f"[USER] 昵称「{args.user_id}」→ {resolved}")
                args.user_id = resolved
            task_id = f"user:{args.user_id}:{fetcher.account.alias}"
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
                        note_id = item.get("note_id") or item.get("id")
                        xsec_token = item.get("xsec_token", "")
                        # 列表接口数据不完整，逐条调详情接口补全
                        try:
                            detail = fetch_note_detail(fetcher, note_id, xsec_token=xsec_token, xsec_source="pc_user")
                            if not detail:
                                raise ValueError("笔记不存在")
                            note_card = detail.get("note_card") or detail
                            note = _normalize_note(note_card)
                        except Exception:
                            # 详情失败则用列表数据兜底
                            note = _normalize_note({
                                "id": note_id,
                                "xsec_token": xsec_token,
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
                print(f'__CRAWL_METRICS__:{{"items_found":{total},"items_new":{total},"items_failed":0}}')
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
    p.add_argument("--speed-mode", choices=["paranoid"], default="paranoid",
                    help="速度模式（当前仅支持 paranoid）")
    p.add_argument("--proxy", default=None)
    p.add_argument("--account", default=None, help="指定账号别名")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xhs", description="小红书爬虫 CLI")
    p.add_argument("--version", action="version", version=f"xiaohongshu-scraper v{__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="安装所有依赖（首次运行自动触发，此命令用于排查）")
    p_setup.set_defaults(func=cmd_setup)

    p_health = sub.add_parser("health", help="系统健康检查（依赖+签名+账号+DB）")
    p_health.set_defaults(func=cmd_health)

    p_login = sub.add_parser("login", help="获取并保存 cookie")
    p_login.add_argument("--prefer",
                          choices=["auto", "rookie", "edge", "chrome", "firefox", "brave",
                                   "native", "native-edge", "native-chrome",
                                   "native-firefox", "native-brave",
                                   "wsl-edge", "wsl-edge-cdp",
                                   "wsl-chrome", "wsl-chrome-cdp", "qr", "manual"],
                          default="auto",
                          help="登录方式（auto=自动选择最优）")
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

    p_corr = sub.add_parser("correct", help="Agent 手动纠错视频转录：列出/读取/写回")
    mx_corr = p_corr.add_mutually_exclusive_group()
    mx_corr.add_argument("--list", action="store_true", help="列出待纠错视频笔记")
    mx_corr.add_argument("--note", default=None, help="指定 note_id（读取或写回；与 --list 互斥）")
    p_corr.add_argument("--limit", type=int, default=50, help="--list 时最大条数（默认 50）")
    p_corr.add_argument("--apply", default=None, help="纠错后的转录文本（写回 video_transcript，标记 agent纠错；须配 --note）")
    p_corr.set_defaults(func=cmd_correct)

    p_ref = sub.add_parser("refresh", help="重抓超过 N 小时的旧笔记（增量更新）")
    p_ref.add_argument("--max-age-hours", type=int, default=24, help="重抓 N 小时前的笔记（默认 24）")
    p_ref.add_argument("--limit", type=int, default=100, help="最多刷新几条（默认 100）")
    _add_common(p_ref)
    p_ref.set_defaults(func=cmd_refresh)

    p_enr = sub.add_parser("enrich", help="补全搜索入库的半成品笔记（逐条调详情 API）")
    p_enr.add_argument("--limit", type=int, default=50, help="最多补全几条（默认 50）")
    _add_common(p_enr)
    p_enr.set_defaults(func=cmd_enrich)

    p_exp = sub.add_parser("export", help="从 DB 导出 MD/CSV/JSON/XLSX")
    p_exp.add_argument("--format", choices=["md", "csv", "json", "xlsx"], default="csv")
    p_exp.add_argument("--note", default=None, help="单篇 MD 时指定 note_id")
    p_exp.add_argument("--user", default=None, help="按 user_id 过滤（CSV/JSON/XLSX）")
    p_exp.set_defaults(func=cmd_export)

    p_acct = sub.add_parser("accounts", help="查看多账号状态")
    p_acct.add_argument("--set-speed", default=None, metavar="alias=mode",
                         help="设置账号专属速率（如 --set-speed account3=slow）")
    p_acct.set_defaults(func=cmd_accounts)

    p_stats = sub.add_parser("stats", help="请求统计")
    p_stats.add_argument("--hours", type=int, default=None)
    p_stats.add_argument("--account", default=None)
    p_stats.set_defaults(func=cmd_stats)

    p_ujs = sub.add_parser("update-js", help="从 cv-cat/Spider_XHS 拉取最新签名 JS")
    p_ujs.add_argument("--dry-run", action="store_true", help="只检查不覆盖")
    p_ujs.set_defaults(func=cmd_update_js)

    p_ufp = sub.add_parser("update-fp", help="全量更新 UA/指纹池/TLS/签名JS")
    p_ufp.add_argument("--dry-run", action="store_true", help="只检查不写入")
    p_ufp.set_defaults(func=cmd_update_fp)

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

    p_ka = sub.add_parser("keepalive", help="多账号 Cookie 自动保活")
    p_ka.add_argument("--daemon", action="store_true", help="守护进程模式（持续运行）")
    p_ka.add_argument("--interval", type=int, default=0,
                       help="守护进程检查间隔（秒，默认 3600 = 1小时）")
    p_ka.add_argument("--account", default=None, help="只保活指定账号（默认所有）")
    p_ka.add_argument("--force", action="store_true", help="强制执行保活（即使 cookie 当前有效）")
    p_ka.set_defaults(func=cmd_keepalive)

    p_az = sub.add_parser("analyze", help="评论情感分析 / 话题聚类")
    p_az.add_argument("--type", choices=["sentiment", "topics"], default="topics",
                      help="分析类型（默认 topics）")
    p_az.add_argument("--note", default=None, help="限定单篇笔记（情感分析）")
    p_az.add_argument("--keyword", default=None, help="限定搜索关键词")
    p_az.add_argument("--user", default=None, help="限定用户 ID（话题聚类）")
    p_az.add_argument("--output", choices=["text", "json"], default="text", help="输出格式")
    p_az.set_defaults(func=cmd_analyze)

    p_av = sub.add_parser("analyze-video", help="视频内容智能分析（语音转文字 + OCR；转录需 Agent 用 correct 纠错）")
    p_av.add_argument("note_id", help="视频笔记 ID（需已入库）")
    p_av.add_argument("--correct-mode", choices=["auto", "openai", "ollama", "none"], default=None,
                      help="LLM 纠错模式（覆盖配置文件）")
    p_av.add_argument("--whisper-model", default=None, help="Whisper 模型（tiny/base/small/medium/large-v3）")
    p_av.add_argument("--frame-interval", type=int, default=None, help="关键帧间隔秒数")
    p_av.add_argument("--max-duration", type=int, default=None,
                      help="最多转录前 N 秒音频（默认 300，0=不限制）")
    p_av.add_argument("--step", nargs="*", default=None,
                      help="分段执行: extract transcribe ocr（不传=全部执行）")
    _add_common(p_av)
    p_av.set_defaults(func=cmd_analyze_video)

    p_sv = sub.add_parser("setup-video", help="交互式配置视频分析")
    p_sv.add_argument("--correct-mode", choices=["auto", "openai", "ollama", "none"], default=None,
                      help="直接指定 LLM 纠错模式（跳过交互）")
    p_sv.add_argument("--whisper-model", default=None, help="直接指定 Whisper 模型")
    p_sv.add_argument("--frame-interval", type=int, default=None, help="关键帧间隔秒数")
    p_sv.set_defaults(func=cmd_setup_video)

    p_ai = sub.add_parser("analyze-images", help="图片 OCR 文字提取")
    p_ai.add_argument("note_id", help="图文笔记 ID（需已入库）")
    _add_common(p_ai)
    p_ai.set_defaults(func=cmd_analyze_images)

    p_sw = sub.add_parser("setup-wizard", help="统一引导向导：配置图片+视频分析")
    p_sw.set_defaults(func=cmd_setup_wizard)

    # ---- run ----
    p_run = sub.add_parser("run", help="按需执行模式：单次搜索+提取，用完即停（比 serve 更安全）")
    p_run.add_argument("--keywords", default="", required=True,
                        help="搜索关键词，逗号分隔（如 'AI工具,AI编程'）")
    p_run.add_argument("--max-notes", type=int, default=20,
                        help="每关键词最多提取笔记数（默认20）")
    p_run.add_argument("--pages", type=int, default=2,
                        help="每关键词最多搜索页数（默认2）")
    _add_common(p_run)  # 包含 --account --sign-mode --speed-mode --proxy
    p_run.set_defaults(func=cmd_run)

    # ---- serve ----
    p_serve = sub.add_parser("serve", help="守护进程模式：自动循环爬取")
    p_serve.add_argument("--interval", type=float, default=6, help="爬取间隔（小时，默认6，支持小数如0.1=6分钟）")
    p_serve.add_argument("--max-pages", type=int, default=5, help="每次最多爬取页数（默认5）")
    p_serve.add_argument("--targets", nargs="+", default=[],
                         help="爬取目标列表（user:uid / feed:category / search:kw）")
    p_serve.add_argument("--proxy", default=None, help="代理 URL（如 http://host:port）")
    p_serve.set_defaults(func=cmd_serve)

    return p


def cmd_run(args):
    """按需执行模式：单次搜索+提取，执行完即退出。

    与 serve 守护模式的核心区别：
    - serve：持续运行，累积行为 profile → 2-3天被检测
    - run：单次任务（30-60分钟），完成后彻底停止 → 无累积 profile

    流程：
    1. 自动选最久未用的可用账号（由 _make_fetcher → next_available 处理）
    2. 设置搜索模式为浏览器 DOM（降低 API 指纹暴露）
    3. 搜索关键词 → 提取 note_id
    4. API 获取笔记详情 + 输出结果
    5. 彻底关闭浏览器 → 退出
    """
    import datetime as _dt

    # ---- 1. 解析关键词 ----
    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    if not keywords:
        print("[RUN] 错误：至少需要一个关键词", file=sys.stderr)
        return 2
    print(f"[RUN] 关键词: {keywords}")
    print(f"[RUN] 每关键词最多 {args.max_notes} 条笔记，最多 {args.pages} 页")
    print(f"[RUN] 模式：浏览器 DOM 搜索 + API 详情获取")

    # ---- 2. 设置浏览器 DOM 搜索模式 ----
    xhs_config.SEARCH_MODE = "dom"
    print(f"[RUN] 搜索模式: {xhs_config.SEARCH_MODE}")

    # ---- 3. 执行搜索 + 提取（账号由 fetch_session 自动选择最久未用） ----
    hb = Heartbeat()
    try:
        with fetch_session(args) as (fetcher, conn):
            total_notes = 0
            for kw_idx, keyword in enumerate(keywords):
                if total_notes >= args.max_notes * len(keywords):
                    break
                print(f"\n[RUN] [{kw_idx+1}/{len(keywords)}] 搜索: '{keyword}'")
                note_ids: list[str] = []
                for page in range(1, args.pages + 1):
                    try:
                        data = fetch_search(fetcher, keyword, page=page)
                    except Exception as e:
                        print(f"[RUN] 搜索失败 page={page}: {e}", file=sys.stderr)
                        break
                    items = data.get("items") or []
                    if not items:
                        print(f"[RUN] page={page} 无结果，停止翻页")
                        break
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
                        total_notes += 1
                        preview = _desc_preview(note)
                        print(f"  [{total_notes}] {note['note_id']} {preview}")
                        if len(note_ids) >= args.max_notes:
                            break
                    xhs_storage.save_search_page(conn, keyword, page, note_ids)
                    conn.commit()
                    if len(note_ids) >= args.max_notes or not data.get("has_more"):
                        break
                print(f"[RUN] '{keyword}' 完成：找到 {len(note_ids)} 条笔记")

            print(f"\n[RUN] 全部完成：共 {total_notes} 条笔记")

            # ---- 4. 记录账号使用（last_used 已由 mark_used 自动更新） ----
            try:
                alias = getattr(fetcher.account, 'alias', None)
                if alias:
                    mgr = xhs_accounts.AccountManager()
                    mgr.save_state()
                    print(f"[RUN] 账号 {alias} 使用完成，建议休息 4-6 小时后再用")
            except Exception:
                pass

            return 0
    finally:
        hb.stop()
        # 恢复默认搜索模式（DOM 优先）
        xhs_config.SEARCH_MODE = "dom"


def cmd_serve(args):
    """守护进程模式：自动循环爬取。每轮：健康检查 → keepalive → 爬取 → 休息。
    """
    import signal
    import time as _time
    import json as _json

    base_s = args.interval * 3600

    def _random_interval(base: float) -> float:
        """对数正态随机间隔，20% 概率长休息，最短 8 分钟。"""
        if random.random() < 0.20:
            interval = base * random.uniform(2.0, 3.5)
        else:
            interval = base * random.lognormvariate(0, 0.3)
        return max(interval, 480)

    interval_s = _random_interval(base_s)

    stop = [False]
    def _stop(sig, frame):
        print("\n[serve] 收到停止信号，等当前任务完成...")
        stop[0] = True
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _stop)

    print(f"[serve] 启动，基准间隔 {args.interval}h（随机抖动），每轮最多 {args.max_pages} 页")

    # 强制 DOM 搜索模式（守护进程必须走浏览器，降低风控风险）
    xhs_config.SEARCH_MODE = "dom"
    print(f"[serve] 搜索模式: {xhs_config.SEARCH_MODE}")

    while not stop[0]:
        print(f"\n{'='*50}")
        print(f"[serve] 新一轮开始 {time.strftime('%Y-%m-%d %H:%M:%S')}")

        # 1. Keepalive: 尝试刷新 cookie
        mgr = xhs_accounts.AccountManager()
        try:
            import xhs_keepalive
            xhs_keepalive.keepalive_all(mgr, force=True)
            print("[serve] Keepalive 完成")
        except Exception as e:
            print(f"[serve] Keepalive 失败: {e}")

        # 2. 获取目标
        targets = args.targets
        if not targets:
            print("[serve] 无可爬目标，请通过 --targets user:uid1 user:uid2 ... 指定")
            _xhs_sleep_or_stop(stop, interval_s)
            continue
        print(f"[serve] 目标列表: {targets}")

        # 3. 串行爬取：逐目标执行，轮换账号
        for idx, target_ident in enumerate(targets):
            if stop[0]:
                break
            # 每次循环前刷新可用账号列表（上一个目标可能触发冷却）
            aliases = [a for a, acc in mgr.accounts.items() if acc.is_available()[0]]
            if not aliases:
                print("[serve] 所有账号已进入冷却，跳过剩余目标")
                break
            if ":" in target_ident:
                kind, value = target_ident.split(":", 1)
            else:
                kind, value = "search", target_ident

            account = aliases[idx % len(aliases)]
            print(f"\n[serve] 爬取 {kind}:{value} (max_pages={args.max_pages}, account={account})")

            try:
                if kind == "search":
                    crawl_args = argparse.Namespace(
                        cmd="crawl-search", keyword=value,
                        max_pages=args.max_pages, resume=True,
                        download=False, analyze=False,
                        sign_mode="auto", speed_mode="paranoid",
                        proxy=args.proxy, account=account,
                    )
                    cmd_crawl_search(crawl_args)
                elif kind == "feed":
                    crawl_args = argparse.Namespace(
                        cmd="crawl-feed", category=value or "recommend",
                        max_pages=args.max_pages, resume=True,
                        download=False, analyze=False,
                        sign_mode="auto", speed_mode="paranoid",
                        proxy=args.proxy, account=account,
                    )
                    cmd_crawl_feed(crawl_args)
                elif kind == "user":
                    crawl_args = argparse.Namespace(
                        cmd="crawl-user", user_id=value,
                        max_pages=args.max_pages, resume=True,
                        download=False, analyze=False,
                        sign_mode="auto", speed_mode="paranoid",
                        proxy=args.proxy, account=account,
                    )
                    cmd_crawl_user(crawl_args)
            except Exception as e:
                print(f"[serve] 爬取失败: {e}")
                _write_serve_alert("xiaohongshu", f"爬取 {target_ident} 失败: {e}")

        # 5. 随机休息
        interval_s = _random_interval(base_s)
        minutes = interval_s / 60
        print(f"\n[serve] 本轮结束，随机休息 {minutes:.0f} 分钟后开始下一轮")
        _xhs_sleep_or_stop(stop, interval_s)

    print("[serve] 已停止")
    return 0


def _write_serve_alert(source_type, message, action=""):
    try:
        alert_path = Path(__file__).resolve().parent.parent / "crawler_alerts.jsonl"
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_type": source_type,
            "severity": "expired",
            "message": message[:300],
            "action_required": action,
        }
        with open(str(alert_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _xhs_sleep_or_stop(stop, seconds):
    elapsed = 0
    chunk = min(10, seconds) if seconds > 0 else 10
    while elapsed < seconds:
        if stop[0]:
            return
        sleep_time = min(chunk, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time


def main(argv: list[str] | None = None) -> int:
    # Fix Windows console encoding for Unicode output
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    # 注册 SIGTERM：收到终止信号时优雅退出（本进程不写 PID 文件）
    import signal
    def _sigterm_handler(sig, frame):
        sys.exit(143)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _sigterm_handler)

    # 首次运行自动安装依赖（延迟到 main() 而非模块级，避免 import 副作用）
    xhs_bootstrap.ensure_ready()

    parser = build_parser()
    args = parser.parse_args(argv)
    rc = 0
    try:
        rc = args.func(args)
    except FatalRiskError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[ABORT] 用户中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[ERR] {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # 命令成功后自动尝试同步到 PG（仅数据写入类命令）
    _SYNC_COMMANDS = {"note", "user", "search", "crawl-search", "crawl-user",
                       "crawl-feed", "feed", "comments", "download", "refresh",
                       "enrich", "run", "serve"}
    if (rc == 0 or rc is None) and getattr(args, 'func', None):
        cmd_name = getattr(args.func, '__name__', '') or ''
        # cmd_name 格式: cmd_xxx
        base_cmd = cmd_name.replace('cmd_', '').replace('_', '-')
        if base_cmd in _SYNC_COMMANDS:
            _try_pg_sync()
            # 只在带 --analyze 的命令后提示纠错（减噪：download/comments/feed 等不提示）
            if getattr(args, "analyze", False):
                _print_correction_hint()

    return rc if rc else 0


if __name__ == "__main__":
    sys.exit(main())
