"""指纹池生成器。

基于 curl_cffi 支持的 Chrome 大版本号和真实 sec-ch-ua 格式，
交叉生成多样化的指纹 profile。
"""

from __future__ import annotations

from itertools import product


# ---- 维度模板 ----

_OS_TEMPLATES = [
    # (user_agent_fragment, sec_ch_ua_platform)
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
        '"macOS"',
    ),
]

# sec-ch-ua 格式变体 — 不同 Chrome 版本使用不同的品牌字符串
# 生成时会用 chrome_major 选择实际存在的格式
_SEC_CH_UA_FORMATS = {
    # 格式名: 品牌字符串模板（第三个 brand 字段）
    "not_a_brand": '"Not-A.Brand";v="24"',
    "not_dot": '"Not.A/Brand";v="99"',
    "not_question": '"Not?A_Brand";v="99"',
    "not_semicolon": '"Not;A=Brand";v="99"',
}

_ACCEPT_LANG_VARIANTS = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.6",
]


def _build_sec_ch_ua(chrome_major: int, brand_variant: str) -> str:
    """构建 sec-ch-ua 字符串。"""
    brand = _SEC_CH_UA_FORMATS.get(brand_variant, _SEC_CH_UA_FORMATS["not_a_brand"])
    return f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", {brand}'


def generate_pool(chrome_major: int, real_sec_ch_ua: str = "") -> list[dict]:
    """基于 chrome_major 生成指纹 profile 列表。

    real_sec_ch_ua: 从 curl_cffi 实际请求提取的 sec-ch-ua（可选）。
    如果提供，将作为第一个 profile 的 sec-ch_ua 基准。

    返回 dict 列表，每个 dict 对应 FingerprintProfile 字段：
    user_agent, sec_ch_ua, impersonate, accept_language, timezone, region
    """
    ver = str(chrome_major)
    impersonate = f"chrome{chrome_major}"

    # sec-ch-ua 变体列表：真实值 + 人工变体
    sec_variants: list[str] = []
    if real_sec_ch_ua:
        sec_variants.append(real_sec_ch_ua)
    for name in _SEC_CH_UA_FORMATS:
        variant = _build_sec_ch_ua(chrome_major, name)
        if variant not in sec_variants:
            sec_variants.append(variant)

    profiles: list[dict] = []

    for (ua_tpl, _platform), sec, lang in product(
        _OS_TEMPLATES, sec_variants, _ACCEPT_LANG_VARIANTS
    ):
        profiles.append({
            "user_agent": ua_tpl.format(ver=ver),
            "sec_ch_ua": sec,
            "impersonate": impersonate,
            "accept_language": lang,
            "timezone": "Asia/Shanghai",
            "region": "CN",
        })

    return profiles
