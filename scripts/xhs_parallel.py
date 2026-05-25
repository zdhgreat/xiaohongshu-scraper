"""多账号并行爬取模块。

每个账号分配一个独立线程，各线程拥有独立的 Fetcher / Signer / DB Connection。
线程间零共享可变状态，安全并行。

任务分配模式：
  - crawl-parallel --users uid1 uid2 ...     每个账号爬一个用户
  - crawl-parallel --keywords kw1 kw2 ...    每个账号搜一个关键词
"""

from __future__ import annotations

import threading
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any

import xhs_accounts
import xhs_config
import xhs_fetcher
import xhs_sign
import xhs_storage


@dataclass
class TaskResult:
    """单个账号的执行结果。"""
    alias: str
    task: str
    status: str = "pending"       # ok / error / skipped
    total: int = 0
    error: str = ""
    comment_count: int = 0
    detail_errors: int = 0
    comment_errors: int = 0
    media_errors: int = 0


def _make_fetcher_for_account(alias: str, speed_mode: str = "paranoid",
                               sign_mode: str = "auto") -> xhs_fetcher.Fetcher:
    """为指定账号创建独立的 Fetcher（禁用自动轮换）。账号专属速率优先。"""
    import xhs_proxy
    mgr = xhs_accounts.AccountManager()
    # 账号专属速率优先于全局参数
    acc = mgr.accounts.get(alias)
    if acc and acc.speed_mode:
        speed_mode = acc.speed_mode
    signer = xhs_sign.make_signer(sign_mode)
    proxy_pool = xhs_proxy.ProxyPool()
    speed = xhs_config.SPEED_PROFILES[speed_mode]
    return xhs_fetcher.Fetcher(
        signer, speed, mgr, proxy_pool,
        force_account=alias,
        sign_mode_label=sign_mode,
        speed_mode_label=speed_mode,
        autonomous=True,
    )


def _enrich_note(fetcher, note: dict, conn) -> None:
    """抓取笔记详情，补全 description / video_url 等完整字段。"""
    from xhs_api import fetch_note_detail, _normalize_note
    nid = note["note_id"]
    token = note.get("xsec_token", "")
    source = note.get("xsec_source", "pc_search")
    try:
        item = fetch_note_detail(fetcher, nid, xsec_token=token, xsec_source=source)
        full = _normalize_note(item)
        if note.get("user_id"):
            full["user_id"] = note["user_id"]
        xhs_storage.upsert_note(conn, full)
    except Exception as e:
        print(f"  详情 {nid[:12]}... 失败（跳过）: {e}", file=sys.stderr)


def _fetch_comments_for_note(fetcher, conn, note_id: str, xsec_token: str,
                              max_pages: int = 5) -> int:
    """抓取单条笔记的评论。返回评论总数。复用调用方的 conn 避免锁冲突。"""
    from xhs_api import fetch_comments, _normalize_comment
    try:
        cursor = ""
        total = 0
        for page in range(1, max_pages + 1):
            data = fetch_comments(fetcher, note_id, xsec_token, cursor=cursor)
            comments = data.get("comments") or []
            if not comments:
                break
            for c in comments:
                norm = _normalize_comment(c, note_id)
                if norm["comment_id"]:
                    xhs_storage.upsert_comment(conn, norm)
                    total += 1
                for sc in (c.get("sub_comments") or []):
                    sn = _normalize_comment(sc, note_id, parent_id=norm.get("comment_id"))
                    if sn["comment_id"]:
                        xhs_storage.upsert_comment(conn, sn)
                        total += 1
            conn.commit()
            cursor = data.get("cursor", "")
            if not data.get("has_more"):
                break
        return total
    except Exception:
        return 0


def _crawl_user_worker(alias: str, user_id: str, max_pages: int,
                        speed_mode: str, sign_mode: str,
                        results: dict[str, TaskResult],
                        download: bool = False, analyze: bool = False):
    """单账号爬取单个用户的 worker 线程。"""
    from xhs_config import Heartbeat
    from xhs_api import fetch_user_info, fetch_user_notes, _normalize_note, _normalize_user
    import xhs_media

    tag = f"[{alias}] user:{user_id[:8]}..."
    print(f"{tag} 线程启动", file=sys.stderr)
    hb = Heartbeat()
    fetcher = _make_fetcher_for_account(alias, speed_mode, sign_mode)
    conn = xhs_storage.connect()
    detail_err = comment_err = media_err = 0

    try:
        fetcher.warmup()

        # 用户信息
        try:
            info = fetch_user_info(fetcher, user_id)
            if info:
                info["user_id"] = user_id
                xhs_storage.upsert_user(conn, _normalize_user(info))
                conn.commit()
                print(f"{tag} 用户: {info.get('nickname', '?')}", file=sys.stderr)
        except Exception as e:
            print(f"{tag} 用户信息失败（继续）: {e}", file=sys.stderr)

        # 笔记列表
        cursor = ""
        total = 0
        note_ids = []
        for page in range(1, max_pages + 1):
            data = fetch_user_notes(fetcher, user_id, cursor=cursor)
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
                note["user_id"] = user_id
                xhs_storage.upsert_note(conn, note)
                note_ids.append(note["note_id"])
                total += 1
            conn.commit()
            cursor = data.get("cursor", "")
            print(f"{tag} [page {page}] +{len(items)} 累计 {total}", file=sys.stderr)
            if not data.get("has_more") or not cursor:
                break

        # 第二轮：抓详情补全数据
        print(f"{tag} 列表完成，开始抓取 {len(note_ids)} 条笔记详情...", file=sys.stderr)
        enriched = 0
        for nid in note_ids:
            row = xhs_storage.get_note(conn, nid)
            if not row:
                continue
            note_dict = dict(row)
            try:
                _enrich_note(fetcher, note_dict, conn)
            except Exception as e:
                detail_err += 1
                if detail_err <= 3:
                    print(f"{tag} 详情失败 {nid[:12]}...: {e}", file=sys.stderr)
            enriched += 1
            if enriched % 20 == 0:
                conn.commit()
                print(f"{tag} 详情进度: {enriched}/{len(note_ids)}", file=sys.stderr)
        conn.commit()
        print(f"{tag} 详情完成: {enriched}/{len(note_ids)} ({detail_err} 失败)", file=sys.stderr)

        # 第三轮：抓评论
        print(f"{tag} 开始抓取 {len(note_ids)} 条笔记评论...", file=sys.stderr)
        comment_total = 0
        for i, nid in enumerate(note_ids):
            row = xhs_storage.get_note(conn, nid)
            if not row:
                continue
            token = row["xsec_token"] or ""
            if not token:
                continue
            try:
                cnt = _fetch_comments_for_note(fetcher, conn, nid, token, max_pages=3)
                comment_total += cnt
            except Exception as e:
                comment_err += 1
                if comment_err <= 3:
                    print(f"{tag} 评论失败 {nid[:12]}...: {e}", file=sys.stderr)
            if (i + 1) % 20 == 0:
                print(f"{tag} 评论进度: {i+1}/{len(note_ids)} 累计 {comment_total} 条", file=sys.stderr)
        print(f"{tag} 评论完成: {comment_total} 条 ({comment_err} 失败)", file=sys.stderr)

        # 第四轮：下载+分析
        if download or analyze:
            print(f"{tag} 开始后处理（download={download}, analyze={analyze}）...", file=sys.stderr)
            for nid in note_ids:
                row = xhs_storage.get_note(conn, nid)
                if not row:
                    continue
                note_dict = dict(row)
                try:
                    xhs_media.post_process_note(note_dict, conn, type('Args', (), {
                        'download': download, 'analyze': analyze,
                    })())
                except Exception as e:
                    media_err += 1
                    if media_err <= 3:
                        print(f"{tag} 后处理失败 {nid[:12]}...: {e}", file=sys.stderr)

        results[alias] = TaskResult(alias, f"user:{user_id}", "ok", total,
                                    comment_count=comment_total,
                                    detail_errors=detail_err,
                                    comment_errors=comment_err,
                                    media_errors=media_err)
        print(f"{tag} 完成: {total} 条笔记, {comment_total} 条评论"
              f" (详情×{detail_err} 评论×{comment_err} 媒体×{media_err})", file=sys.stderr)

    except Exception as e:
        results[alias] = TaskResult(alias, f"user:{user_id}", "error", 0, str(e))
        print(f"{tag} 错误: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        hb.stop()
        fetcher.close()
        conn.close()


def _crawl_search_worker(alias: str, keyword: str, max_pages: int,
                          speed_mode: str, sign_mode: str,
                          results: dict[str, TaskResult],
                          download: bool = False, analyze: bool = False):
    """单账号搜索爬取的 worker 线程。"""
    from xhs_config import Heartbeat
    from xhs_api import fetch_search, _normalize_note
    import xhs_media

    tag = f"[{alias}] search:'{keyword}'"
    print(f"{tag} 线程启动", file=sys.stderr)
    hb = Heartbeat()
    fetcher = _make_fetcher_for_account(alias, speed_mode, sign_mode)
    conn = xhs_storage.connect()
    detail_err = comment_err = media_err = 0

    try:
        fetcher.warmup()

        total = 0
        all_note_ids = []
        for page in range(1, max_pages + 1):
            data = fetch_search(fetcher, keyword, page=page)
            items = data.get("items") or []
            if not items:
                print(f"{tag} [page {page}] 空结果", file=sys.stderr)
                break
            page_ids = []
            for item in items:
                if item.get("model_type") != "note":
                    continue
                item.setdefault("xsec_source", "pc_search")
                note = _normalize_note(item)
                if not note["note_id"]:
                    continue
                xhs_storage.upsert_note(conn, note)
                page_ids.append(note["note_id"])
                all_note_ids.append(note["note_id"])
                total += 1
            conn.commit()
            print(f"{tag} [page {page}] +{len(page_ids)} 累计 {total}", file=sys.stderr)
            if not data.get("has_more"):
                break

        # 第二轮：抓详情补全数据
        print(f"{tag} 列表完成，开始抓取 {len(all_note_ids)} 条笔记详情...", file=sys.stderr)
        enriched = 0
        for nid in all_note_ids:
            row = xhs_storage.get_note(conn, nid)
            if not row:
                continue
            note_dict = dict(row)
            try:
                _enrich_note(fetcher, note_dict, conn)
            except Exception as e:
                detail_err += 1
                if detail_err <= 3:
                    print(f"{tag} 详情失败 {nid[:12]}...: {e}", file=sys.stderr)
            enriched += 1
            if enriched % 20 == 0:
                conn.commit()
                print(f"{tag} 详情进度: {enriched}/{len(all_note_ids)}", file=sys.stderr)
        conn.commit()
        print(f"{tag} 详情完成: {enriched}/{len(all_note_ids)} ({detail_err} 失败)", file=sys.stderr)

        # 第三轮：抓评论
        print(f"{tag} 开始抓取 {len(all_note_ids)} 条笔记评论...", file=sys.stderr)
        comment_total = 0
        for i, nid in enumerate(all_note_ids):
            row = xhs_storage.get_note(conn, nid)
            if not row:
                continue
            token = row["xsec_token"] or ""
            if not token:
                continue
            try:
                cnt = _fetch_comments_for_note(fetcher, conn, nid, token, max_pages=3)
                comment_total += cnt
            except Exception as e:
                comment_err += 1
                if comment_err <= 3:
                    print(f"{tag} 评论失败 {nid[:12]}...: {e}", file=sys.stderr)
            if (i + 1) % 20 == 0:
                print(f"{tag} 评论进度: {i+1}/{len(all_note_ids)} 累计 {comment_total} 条", file=sys.stderr)
        print(f"{tag} 评论完成: {comment_total} 条 ({comment_err} 失败)", file=sys.stderr)

        # 第四轮：下载+分析
        if download or analyze:
            print(f"{tag} 开始后处理（download={download}, analyze={analyze}）...", file=sys.stderr)
            for nid in all_note_ids:
                row = xhs_storage.get_note(conn, nid)
                if not row:
                    continue
                note_dict = dict(row)
                try:
                    xhs_media.post_process_note(note_dict, conn, type('Args', (), {
                        'download': download, 'analyze': analyze,
                    })())
                except Exception as e:
                    media_err += 1
                    if media_err <= 3:
                        print(f"{tag} 后处理失败 {nid[:12]}...: {e}", file=sys.stderr)

        results[alias] = TaskResult(alias, f"search:{keyword}", "ok", total,
                                    comment_count=comment_total,
                                    detail_errors=detail_err,
                                    comment_errors=comment_err,
                                    media_errors=media_err)
        print(f"{tag} 完成: {total} 条笔记, {comment_total} 条评论"
              f" (详情×{detail_err} 评论×{comment_err} 媒体×{media_err})", file=sys.stderr)

    except Exception as e:
        results[alias] = TaskResult(alias, f"search:{keyword}", "error", 0, str(e))
        print(f"{tag} 错误: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        hb.stop()
        fetcher.close()
        conn.close()


def run_parallel(task_type: str, tasks: list[str], max_pages: int,
                 speed_mode: str = "paranoid", sign_mode: str = "auto",
                 download: bool = False, analyze: bool = False) -> int:
    """并行执行入口。将 tasks 分配给可用账号，启动多线程。

    task_type: "user" 或 "search"
    tasks: user_id 列表 或 keyword 列表
    返回: 0=全部成功, 1=部分失败, 2=全部失败
    """
    import xhs_login

    # 获取可用账号
    mgr = xhs_accounts.AccountManager()
    aliases = [a for a, acc in mgr.accounts.items() if acc.is_available()[0]]
    if not aliases:
        print("[ERR] 没有可用账号", file=sys.stderr)
        return 2

    # 验证所有账号 cookie
    print(f"[PARALLEL] 验证 {len(aliases)} 个账号...", file=sys.stderr)
    for alias, acc in list(mgr.accounts.items()):
        try:
            valid, _, updated = xhs_login.validate_cookies_online(
                acc.cookies, fingerprint=getattr(acc, 'fingerprint', None))
            if valid:
                acc.cookies = updated
                acc.save_cookies()
                print(f"  [{alias:15s}] 有效", file=sys.stderr)
            else:
                print(f"  [{alias:15s}] 无效（跳过）", file=sys.stderr)
                if alias in aliases:
                    aliases.remove(alias)
        except Exception:
            print(f"  [{alias:15s}] 网络异常（保留）", file=sys.stderr)

    if not aliases:
        print("[ERR] 所有账号 cookie 无效", file=sys.stderr)
        return 2

    # 分配任务到账号（轮询）
    assignments: dict[str, list[str]] = {}
    for i, task in enumerate(tasks):
        alias = aliases[i % len(aliases)]
        assignments.setdefault(alias, []).append(task)

    print(f"[PARALLEL] {len(tasks)} 个任务 → {len(assignments)} 个账号", file=sys.stderr)
    for alias, alias_tasks in assignments.items():
        print(f"  [{alias}] → {alias_tasks}", file=sys.stderr)

    # 启动线程
    results: dict[str, TaskResult] = {}
    threads = []

    for alias, alias_tasks in assignments.items():
        for task in alias_tasks:
            if task_type == "user":
                target = _crawl_user_worker
            else:
                target = _crawl_search_worker

            # 同一账号多个任务时，用 alias+task 作为唯一 key
            key = f"{alias}:{task}"
            t = threading.Thread(
                target=target,
                args=(alias, task, max_pages, speed_mode, sign_mode,
                      results, download, analyze),
                name=f"xhs-{alias}-{task[:10]}",
                daemon=True,
            )
            threads.append(t)

    for t in threads:
        t.start()
    # 超时保护：每个任务最多 4 小时（slow 模式 100 条笔记约需 3 小时）
    timeout = max_pages * 180 * 60  # 每页最多 3 小时
    timeout = max(timeout, 3600)    # 下限 1 小时
    for t in threads:
        t.join(timeout=timeout)
        if t.is_alive():
            print(f"[PARALLEL] 警告: 线程 {t.name} 超时未完成（{timeout}s），继续等待...",
                  file=sys.stderr)

    # 汇总结果
    print("\n" + "=" * 50, file=sys.stderr)
    print("[PARALLEL] 执行结果汇总:", file=sys.stderr)
    ok_count = 0
    total_notes = 0
    for key, r in results.items():
        icon = "OK" if r.status == "ok" else "ERR"
        errs = ""
        if r.status == "ok" and (r.detail_errors or r.comment_errors or r.media_errors):
            errs = f" [详情×{r.detail_errors} 评论×{r.comment_errors} 媒体×{r.media_errors}]"
        print(f"  [{icon}] {r.alias:15s} {r.task:30s} → {r.total} 条{errs}"
              + (f" ({r.error})" if r.error else ""),
              file=sys.stderr)
        if r.status == "ok":
            ok_count += 1
        total_notes += r.total

    failed = len(results) - ok_count
    print(f"\n[PARALLEL] 总计: {total_notes} 条入库, {ok_count} 成功, {failed} 失败",
          file=sys.stderr)

    if ok_count == 0:
        return 2
    elif failed > 0:
        return 1
    return 0
