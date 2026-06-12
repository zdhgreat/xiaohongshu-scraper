"""统一配置 + 路径常量 + 共享工具函数。

将散落在 xhs.py / xhs_accounts.py / xhs_login.py / xhs_storage.py / xhs_log.py
中的重复常量、路径、工具函数集中到一个模块。

支持 data/config.json 可选外部覆盖。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 根路径
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 网络常量
# ---------------------------------------------------------------------------
BASE = "https://edith.xiaohongshu.com"
WEB_BASE = "https://www.xiaohongshu.com"

# ---------------------------------------------------------------------------
# 核心参数
# ---------------------------------------------------------------------------
DAILY_HARD_CAP: int = 80
IP_DAILY_CAP: int = 80             # 同一 IP 每日总 API 请求上限（所有账号合计）
DOM_SEARCH_DAILY_CAP: int = 100   # DOM 浏览器搜索日上限（独立计数，风控风险极低）
COOKIE_REFRESH_EVERY: int = 20  # 已废弃：实际刷新间隔由 xhs_fetcher._maybe_refresh_cookies() 内部随机决定（15-25 次），此常量仅保留用于 data/config.json 兼容
IMPERSONATE_PROFILE: str = "chrome136"
IP_RATE_LIMIT: int = 10
IP_RATE_WINDOW: int = 60

# 搜索 API 小时配额（IP 级别，预防 -104 封锁）
SEARCH_HOURLY_QUOTA: int = int(os.getenv("XHS_SEARCH_HOURLY_QUOTA", "3"))

# 搜索模式: "api" = 优先API调用（需要签名，风控风险更高）, "dom" = 始终走浏览器DOM搜索（更安全）
SEARCH_MODE: str = os.getenv("XHS_SEARCH_MODE", "dom")

# ---------------------------------------------------------------------------
# Keepalive：Cookie 自动保活
# ---------------------------------------------------------------------------
KEEPALIVE_PROFILE_TIMEOUT_S: int = 30    # Profile 恢复超时（秒）
KEEPALIVE_DAEMON_INTERVAL_S: int = 7200  # 守护进程默认间隔（秒，实际加抖动）
KEEPALIVE_LOGIN_WAIT_S: int = 8          # 等待 session 恢复（秒）
KEEPALIVE_FAIL_COOLDOWN_S: int = 7200    # 保活失败的短冷却（秒）

# ---------------------------------------------------------------------------
# 静默时段：深夜自动降速（模拟人类作息）
# ---------------------------------------------------------------------------
QUIET_HOURS_START: int = 1               # 静默开始（本地时间 24h 制）
QUIET_HOURS_END: int = 6                 # 静默结束
QUIET_HOURS_MULTIPLIER: float = float("inf")  # 静默期间完全停止

# ---------------------------------------------------------------------------
# 重登录：cookie 失效自动恢复
# ---------------------------------------------------------------------------
RELOGIN_MAX_ATTEMPTS: int = 3            # 每账号重登录最大尝试次数
RELOGIN_COOLDOWN_MIN: int = 15           # 重登录失败后冷却（分钟）
RELOGIN_PREDICTIVE_INTERVAL: int = 10    # 预测式检查：每 N 次请求触发一次
COOKIE_PERSIST_INTERVAL: int = 20        # 每 N 次请求持久化 cookie 到磁盘

# ---------------------------------------------------------------------------
# Feed 分类
# ---------------------------------------------------------------------------
FEED_CATEGORIES: dict[str, str] = {
    "recommend": "homefeed_recommend",
    "food": "homefeed.food_v3",
    "fashion": "homefeed.fashion_v3",
    "travel": "homefeed.travel_v3",
    "beauty": "homefeed.beauty_v3",
    "fitness": "homefeed.fitness_v3",
}

# ---------------------------------------------------------------------------
# Speed mode — burst + rest 模型（比均匀分布更像人）
# ---------------------------------------------------------------------------

@dataclass
class SpeedProfile:
    burst_size: tuple[int, int]      # 一波连续多少请求
    burst_gap: tuple[float, float]   # 一波内每两个请求间隔
    rest_gap: tuple[float, float]    # 两波之间停顿（"在看内容"）
    long_rest_every: int             # 多少次请求后强制长停
    long_rest: tuple[float, float]   # 长停时长（"喝水离开"）


SPEED_PROFILES: dict[str, SpeedProfile] = {
    # paranoid：唯一速度 — 每次 1 请求 → 停 4-10min → 每 15 次强休 15-30min
    "paranoid": SpeedProfile((1, 1), (40, 80), (240, 600), 15, (900, 1800)),
}


# ---------------------------------------------------------------------------
# Heartbeat：防止 HTTP 连接静默超时
# ---------------------------------------------------------------------------

class Heartbeat:
    """后台守护线程，定期输出到 stderr 防止 HTTP 连接被服务端判定为静默。
    适用于 Kimi/国产 API 等 60s 静默超时的场景。"""
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
# User-Agent / 请求头
# ---------------------------------------------------------------------------
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

SEC_CH_UA: str = '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'

# ---------------------------------------------------------------------------
# 设备指纹池 — 每个账号分配独立指纹，避免多号同设备
# ---------------------------------------------------------------------------

@dataclass
class FingerprintProfile:
    user_agent: str
    sec_ch_ua: str
    impersonate: str
    accept_language: str
    timezone: str = "Asia/Shanghai"
    region: str = "CN"

FINGERPRINT_POOL: list[FingerprintProfile] = [
    # Chrome 136 / Windows — 4 个变体
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not?A_Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not/A/Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.9,en;q=0.7",
    ),
    # Chrome 136 / Mac — 3 个变体
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.6",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="136", "Google Chrome";v="136", "Not?A_Brand";v="99"',
        impersonate="chrome136",
        accept_language="zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
    ),
    # Chrome 133 / Windows — 3 个变体（模拟稍旧但常见的版本）
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="133", "Google Chrome";v="133", "Not.A/Brand";v="99"',
        impersonate="chrome133a",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="133", "Google Chrome";v="133", "Not.A/Brand";v="99"',
        impersonate="chrome133a",
        accept_language="zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="133", "Google Chrome";v="133", "Not?A_Brand";v="99"',
        impersonate="chrome133a",
        accept_language="zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    # Chrome 133 / Mac — 2 个变体
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="133", "Google Chrome";v="133", "Not.A/Brand";v="99"',
        impersonate="chrome133a",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    FingerprintProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="133", "Google Chrome";v="133", "Not.A/Brand";v="99"',
        impersonate="chrome133a",
        accept_language="zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.6",
    ),
]


def assign_fingerprint(alias: str) -> FingerprintProfile:
    """基于账号 alias 确定性分配指纹（跨 session 一致）。"""
    import hashlib
    idx = int(hashlib.md5(alias.encode()).hexdigest(), 16) % len(FINGERPRINT_POOL)
    return FINGERPRINT_POOL[idx]


def base_headers() -> dict[str, str]:
    """完整模仿 Chrome 请求头集合。

    sec-ch-ua / user-agent / sec-ch-ua-platform / accept-language
    由 Fetcher._apply_fingerprint() 根据账号指纹动态覆盖，
    这里只提供默认值（指纹未加载时的兜底）。
    """
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "origin": WEB_BASE,
        "referer": WEB_BASE + "/",
        "sec-ch-ua": SEC_CH_UA,       # 兜底值，会被指纹覆盖
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',  # 兜底值，会被指纹覆盖
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,     # 兜底值，会被指纹覆盖
    }

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
DATA_DIR: Path = ROOT / "data"
COOKIES_PATH: Path = DATA_DIR / "cookies.json"
ACCOUNTS_DIR: Path = DATA_DIR / "accounts"
ACCOUNTS_STATE: Path = DATA_DIR / "accounts_state.json"
DB_PATH: Path = DATA_DIR / "xhs.db"
OUTPUT_DIR: Path = DATA_DIR / "output"
MEDIA_DIR: Path = DATA_DIR / "media"
LOG_PATH: Path = DATA_DIR / "runs.jsonl"


def sanitize_filename(name: str, max_len: int = 50) -> str:
    """清理字符串为合法文件/目录名。"""
    import re
    # 移除控制字符（0x00-0x1F, 0x7F）
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    # 移除文件系统不合法字符 + 常见标点
    name = re.sub(r'[<>:"/\\|?*!@#$%^&\n\r\t]', '', name)
    name = name.strip(' .')
    # 多个空格/下划线合并
    name = re.sub(r'[\s_]+', '_', name)
    # Windows 保留名称
    _WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
                     'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3',
                     'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    if name.upper() in _WIN_RESERVED:
        name = f"_{name}"
    return name[:max_len]


def note_media_dir(note_id: str, conn=None) -> Path:
    """计算笔记的可读媒体路径: media/<博主名>/<笔记标题>/。

    需要传 conn 来查 DB 获取博主名和标题。
    查不到时 fallback 到 media/<note_id>/。
    """
    if conn is not None:
        try:
            row = conn.execute("SELECT n.title, n.user_id, u.nickname "
                               "FROM notes n LEFT JOIN users u ON n.user_id = u.user_id "
                               "WHERE n.note_id = ?", (note_id,)).fetchone()
            if row:
                title = row[0] or ""
                nickname = row[2] or ""
                user_id = row[1] or ""
                if title:
                    safe_title = sanitize_filename(title, 60)
                    if nickname:
                        safe_author = sanitize_filename(nickname, 30)
                        return MEDIA_DIR / safe_author / safe_title
                    elif user_id:
                        return MEDIA_DIR / user_id / safe_title
        except Exception:
            pass
    return MEDIA_DIR / note_id

# ---------------------------------------------------------------------------
# Cookie 必需键（缺失则判定无效，不可登录）
# ---------------------------------------------------------------------------
REQUIRED_COOKIE_KEYS: set[str] = {"web_session", "a1"}

# Cookie 建议键（服务端 Set-Cookie 动态生成，缺失时风控风险升高但不阻断）
OPTIONAL_COOKIE_KEYS: set[str] = {"websectiga", "sec_poison_id", "webId"}

# ---------------------------------------------------------------------------
# 共享工具函数
# ---------------------------------------------------------------------------

def restrict_file(path: Path | str) -> None:
    """限制文件权限为仅当前用户可读写。Unix: chmod 0600; Windows: 移除继承权限。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r",
                 f"{os.environ.get('USERNAME', '%USERNAME%')}:F"],
                check=True, capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            pass

# ---------------------------------------------------------------------------
# 外部配置加载（可选）
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """从 data/config.json 加载外部配置，覆盖默认值。

    config.json 为可选文件，不存在时返回空 dict。
    """
    config_path = DATA_DIR / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def apply_config() -> None:
    """将 data/config.json 中的值应用到模块级常量。

    只覆盖已识别的配置项，未识别的忽略。
    """
    cfg = load_config()

    global DAILY_HARD_CAP, IP_DAILY_CAP, DOM_SEARCH_DAILY_CAP, COOKIE_REFRESH_EVERY, IMPERSONATE_PROFILE
    global USER_AGENT, SEC_CH_UA, SEARCH_MODE

    if "daily_hard_cap" in cfg:
        DAILY_HARD_CAP = int(cfg["daily_hard_cap"])
    if "ip_daily_cap" in cfg:
        IP_DAILY_CAP = int(cfg["ip_daily_cap"])
    if "dom_search_daily_cap" in cfg:
        DOM_SEARCH_DAILY_CAP = int(cfg["dom_search_daily_cap"])
    if "cookie_refresh_every" in cfg:
        COOKIE_REFRESH_EVERY = int(cfg["cookie_refresh_every"])
    if "impersonate_profile" in cfg:
        IMPERSONATE_PROFILE = str(cfg["impersonate_profile"])
    if "user_agent" in cfg:
        USER_AGENT = str(cfg["user_agent"])
    if "sec_ch_ua" in cfg:
        SEC_CH_UA = str(cfg["sec_ch_ua"])
    if "search_mode" in cfg:
        mode = str(cfg["search_mode"])
        if mode in ("api", "dom"):
            SEARCH_MODE = mode

    # Speed profile 覆盖（仅 paranoid）
    if "speed_profiles" in cfg:
        for name, vals in cfg["speed_profiles"].items():
            if name == "paranoid" and isinstance(vals, dict):
                sp = SPEED_PROFILES["paranoid"]
                if "burst_size" in vals:
                    sp.burst_size = tuple(vals["burst_size"])
                if "burst_gap" in vals:
                    sp.burst_gap = tuple(vals["burst_gap"])
                if "rest_gap" in vals:
                    sp.rest_gap = tuple(vals["rest_gap"])
                if "long_rest_every" in vals:
                    sp.long_rest_every = int(vals["long_rest_every"])
                if "long_rest" in vals:
                    sp.long_rest = tuple(vals["long_rest"])

    # Keepalive 配置
    _ka = cfg.get("keepalive", {})
    if _ka:
        global KEEPALIVE_PROFILE_TIMEOUT_S, KEEPALIVE_DAEMON_INTERVAL_S
        global KEEPALIVE_LOGIN_WAIT_S, KEEPALIVE_FAIL_COOLDOWN_S
        if "profile_timeout_s" in _ka:
            KEEPALIVE_PROFILE_TIMEOUT_S = int(_ka["profile_timeout_s"])
        if "daemon_interval_s" in _ka:
            KEEPALIVE_DAEMON_INTERVAL_S = int(_ka["daemon_interval_s"])
        if "login_wait_s" in _ka:
            KEEPALIVE_LOGIN_WAIT_S = int(_ka["login_wait_s"])
        if "fail_cooldown_s" in _ka:
            KEEPALIVE_FAIL_COOLDOWN_S = int(_ka["fail_cooldown_s"])

    # 静默时段配置
    _qh = cfg.get("quiet_hours", {})
    if _qh:
        global QUIET_HOURS_START, QUIET_HOURS_END, QUIET_HOURS_MULTIPLIER
        if "start" in _qh:
            QUIET_HOURS_START = int(_qh["start"])
        if "end" in _qh:
            QUIET_HOURS_END = int(_qh["end"])
        if "multiplier" in _qh:
            QUIET_HOURS_MULTIPLIER = float(_qh["multiplier"])

    # 重登录配置
    _rl = cfg.get("relogin", {})
    if _rl:
        global RELOGIN_MAX_ATTEMPTS, RELOGIN_COOLDOWN_MIN, RELOGIN_PREDICTIVE_INTERVAL
        if "max_attempts" in _rl:
            RELOGIN_MAX_ATTEMPTS = int(_rl["max_attempts"])
        if "cooldown_min" in _rl:
            RELOGIN_COOLDOWN_MIN = int(_rl["cooldown_min"])
        if "predictive_interval" in _rl:
            RELOGIN_PREDICTIVE_INTERVAL = int(_rl["predictive_interval"])
    if "cookie_persist_interval" in cfg:
        global COOKIE_PERSIST_INTERVAL
        COOKIE_PERSIST_INTERVAL = int(cfg["cookie_persist_interval"])

    # 指纹池外部覆盖（由 updater 模块写入 data/config.json）
    if "fingerprint_pool" in cfg:
        pool_data = cfg["fingerprint_pool"]
        if isinstance(pool_data, list) and len(pool_data) > 0:
            FINGERPRINT_POOL.clear()
            FINGERPRINT_POOL.extend([
                FingerprintProfile(**fp) for fp in pool_data
            ])
            print(
                f"[CONFIG] 指纹池已从 config.json 加载（{len(FINGERPRINT_POOL)} 个 profile）",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# 启动时自动加载外部配置（data/config.json）
# ---------------------------------------------------------------------------
try:
    apply_config()
except Exception as _e:
    print(f"[CONFIG] 外部配置加载失败（使用默认值）: {_e}", file=sys.stderr)
