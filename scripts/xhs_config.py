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
DAILY_HARD_CAP: int = 500
COOKIE_REFRESH_EVERY: int = 20
IMPERSONATE_PROFILE: str = "chrome131"
IP_RATE_LIMIT: int = 10
IP_RATE_WINDOW: int = 60

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
    # normal：连发 3-6 个 → 停 20-60s → 再来一波；每 50 次强休 5-10min
    "normal":   SpeedProfile((3, 6), (2.5, 5.5), (20, 60), 50, (300, 600)),
    # slow：连发 2-4 个 → 停 60-150s → 再来一波；每 30 次强休 10-20min
    "slow":     SpeedProfile((2, 4), (5, 12),    (60, 150), 30, (600, 1200)),
    # paranoid：连发 1-2 个 → 停 3-8min → 再来一波；每 20 次强休 30min+
    "paranoid": SpeedProfile((1, 2), (15, 30),   (180, 480), 20, (1500, 2400)),
}

SPEED_DOWNSHIFT: dict[str, str] = {
    "normal": "slow",
    "slow": "paranoid",
    "paranoid": "paranoid",
}

# ---------------------------------------------------------------------------
# User-Agent / 请求头
# ---------------------------------------------------------------------------
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

SEC_CH_UA: str = '"Chromium";v="131", "Not_A Brand";v="24", "Microsoft Edge";v="131"'

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
    # 0: Edge 131 / Windows
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        sec_ch_ua='"Chromium";v="131", "Not_A Brand";v="24", "Microsoft Edge";v="131"',
        impersonate="chrome131",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    # 1: Chrome 131 / Windows
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        impersonate="chrome131",
        accept_language="zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
    ),
    # 2: Chrome 131 / Mac
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        impersonate="chrome131",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    # 3: Edge 131 / Mac
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        sec_ch_ua='"Chromium";v="131", "Not_A Brand";v="24", "Microsoft Edge";v="131"',
        impersonate="chrome131",
        accept_language="zh-CN,zh;q=0.9,en;q=0.7",
    ),
    # 4: Chrome 131 / Windows (alternative)
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
        impersonate="chrome131",
        accept_language="zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
]


def assign_fingerprint(alias: str) -> FingerprintProfile:
    """基于账号 alias 确定性分配指纹（跨 session 一致）。"""
    import hashlib
    idx = int(hashlib.md5(alias.encode()).hexdigest(), 16) % len(FINGERPRINT_POOL)
    return FINGERPRINT_POOL[idx]


def base_headers() -> dict[str, str]:
    """完整模仿 Chrome 131 请求头集合。"""
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "origin": WEB_BASE,
        "referer": WEB_BASE + "/",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
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
# Cookie 必需键
# ---------------------------------------------------------------------------
REQUIRED_COOKIE_KEYS: set[str] = {"web_session", "a1"}

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

    global DAILY_HARD_CAP, COOKIE_REFRESH_EVERY, IMPERSONATE_PROFILE
    global USER_AGENT, SEC_CH_UA

    if "daily_hard_cap" in cfg:
        DAILY_HARD_CAP = int(cfg["daily_hard_cap"])
    if "cookie_refresh_every" in cfg:
        COOKIE_REFRESH_EVERY = int(cfg["cookie_refresh_every"])
    if "impersonate_profile" in cfg:
        IMPERSONATE_PROFILE = str(cfg["impersonate_profile"])
    if "user_agent" in cfg:
        USER_AGENT = str(cfg["user_agent"])
    if "sec_ch_ua" in cfg:
        SEC_CH_UA = str(cfg["sec_ch_ua"])

    # Speed profile 覆盖
    if "speed_profiles" in cfg:
        for name, vals in cfg["speed_profiles"].items():
            if name in SPEED_PROFILES and isinstance(vals, dict):
                sp = SPEED_PROFILES[name]
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
