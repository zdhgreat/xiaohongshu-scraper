"""结构化运行日志：每次请求一行 JSON 落 data/runs.jsonl。

用于事后复盘风控规律、调优 speed-mode、账号健康度监控。

字段：
  ts            ISO 时间戳
  api           接口路径
  method        GET/POST
  status        HTTP 状态码
  code          业务 code（success.code）
  msg           业务 msg
  duration_ms   耗时（ms）
  sign_mode     embed-js / playwright / py-port
  speed_mode    normal / slow / paranoid
  account       账号别名（默认 default）
  proxy         代理标签（None 或 host:port）
  was_rate_limited  bool（460/461/429/-101 等）
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xhs_config import LOG_PATH


def log_request(
    api: str,
    method: str,
    status: int,
    code: int | None,
    msg: str,
    duration_ms: int,
    *,
    sign_mode: str = "",
    speed_mode: str = "",
    account: str = "default",
    proxy: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    rate_codes = {460, 461, 429, -100, -101, -102, -104, -105, -109, -110}
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api": api,
        "method": method,
        "status": status,
        "code": code,
        "msg": msg[:80] if msg else "",
        "duration_ms": duration_ms,
        "sign_mode": sign_mode,
        "speed_mode": speed_mode,
        "account": account,
        "proxy": proxy,
        "was_rate_limited": status in rate_codes or (code is not None and code in rate_codes),
    }
    if extra:
        record.update(extra)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[LOG] 写日志失败：{e}", file=sys.stderr)

    # 风控事件：本地日志已记录（run_single adapter 会同步到 Hub PG）
    if record["was_rate_limited"]:
        print(f"[RISK] code={code} api={api} account={account} msg={msg[:60]}", file=sys.stderr)


_MAX_LOG_BYTES = 50 * 1024 * 1024  # 50 MB
_MAX_LOG_FILES = 3


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < _MAX_LOG_BYTES:
            return
    except OSError:
        return
    # 用进程 PID 避免多进程轮转冲突
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rotated = path.with_name(f"runs.{ts}.{os.getpid()}.jsonl")
    try:
        path.rename(rotated)
    except OSError:
        return
    # 清理旧的历史文件（只保留最新的 N 个）
    try:
        history = sorted(path.parent.glob("runs.*.jsonl"), reverse=True)
        for old in history[_MAX_LOG_FILES:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def stats(
    hours: int | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """读 runs.jsonl 汇总。"""
    if not LOG_PATH.exists():
        return {"records": 0, "msg": "no log yet"}
    cutoff = None
    if hours:
        cutoff = time.time() - hours * 3600

    total = 0
    ok = 0
    rate_limited = 0
    by_api: Counter[str] = Counter()
    by_status: Counter[int] = Counter()
    by_code: Counter[int] = Counter()
    by_account: Counter[str] = Counter()
    durations: list[int] = []
    first_ts: str | None = None
    last_ts: str | None = None

    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff:
                # 解析 ts 比较
                try:
                    rt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
                    if rt < cutoff:
                        continue
                except Exception:
                    pass
            if account and r.get("account") != account:
                continue
            total += 1
            first_ts = first_ts or r["ts"]
            last_ts = r["ts"]
            if r.get("status") == 200 and not r.get("was_rate_limited"):
                ok += 1
            if r.get("was_rate_limited"):
                rate_limited += 1
            by_api[r.get("api", "?")] += 1
            by_status[r.get("status", 0)] += 1
            by_code[r.get("code") or 0] += 1
            by_account[r.get("account", "default")] += 1
            durations.append(r.get("duration_ms", 0))

    avg_ms = sum(durations) // len(durations) if durations else 0
    return {
        "records": total,
        "ok": ok,
        "rate_limited": rate_limited,
        "rate_limited_pct": round(rate_limited * 100 / total, 1) if total else 0,
        "avg_duration_ms": avg_ms,
        "by_api": dict(by_api.most_common(10)),
        "by_status": dict(by_status),
        "by_account": dict(by_account),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def print_stats(s: dict[str, Any]) -> None:
    if s.get("records", 0) == 0:
        print(s.get("msg", "no data"))
        return
    print(f"=== 运行统计（{s['first_ts']} ~ {s['last_ts']}）===")
    print(f"  总请求数: {s['records']}")
    print(f"  成功: {s['ok']}")
    print(f"  风控触发: {s['rate_limited']} ({s['rate_limited_pct']}%)")
    print(f"  平均耗时: {s['avg_duration_ms']} ms")
    print(f"  按账号: {s['by_account']}")
    print(f"  按状态码: {s['by_status']}")
    print(f"  TOP 接口:")
    for api, n in s["by_api"].items():
        print(f"    {n:>5} × {api}")


def recent_risk_score(minutes: int = 30) -> dict[str, Any]:
    """读取最近 N 分钟的风控事件密度，用于实时风险评分。

    返回: {"total": int, "risk": int, "density": float, "level": str}
    - density = risk事件数 / 分钟数
    - level: "safe" | "elevated" | "high" | "critical"
    """
    if not LOG_PATH.exists():
        return {"total": 0, "risk": 0, "density": 0.0, "level": "safe"}
    cutoff = time.time() - minutes * 60
    total = 0
    risk = 0
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                rt = datetime.fromisoformat(
                    r["ts"].replace("Z", "+00:00")).timestamp()
                if rt < cutoff:
                    continue
            except Exception:
                continue
            total += 1
            if r.get("was_rate_limited"):
                risk += 1
    density = risk / minutes if minutes > 0 else 0
    if density >= 0.5:
        level = "critical"
    elif density >= 0.2:
        level = "high"
    elif density >= 0.05:
        level = "elevated"
    else:
        level = "safe"
    return {"total": total, "risk": risk, "density": round(density, 3), "level": level}
