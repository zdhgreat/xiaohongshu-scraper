"""评论情感分析 & 话题聚类 — 基于已入库数据。

依赖：snownlp（中文情感分析）、jieba（分词 + 关键词提取）
若未安装则优雅降级为简单统计。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

import xhs_storage

# ---------------------------------------------------------------------------
# 可选依赖：snownlp / jieba
# ---------------------------------------------------------------------------

_has_snownlp = False
_has_jieba = False

try:
    from snownlp import SnowNLP  # type: ignore
    _has_snownlp = True
except ImportError:
    pass

try:
    import jieba  # type: ignore
    import jieba.analyse  # type: ignore
    _has_jieba = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# 情感分析
# ---------------------------------------------------------------------------

# 简单中文正面/负面词库（snownlp 不可用时的降级方案）
# 使用 2+ 字词组，避免单字子字符串误判（如"好"匹配"好坏"、"差"匹配"差距"）
_POSITIVE_WORDS = {"好看", "不错", "推荐", "喜欢", "漂亮", "实用", "好吃", "好喝", "满意",
                   "惊喜", "值得", "优秀", "绝了", "神仙", "超赞", "点赞", "太棒", "很美",
                   "真好", "很棒", "很赞", "超好", "超美", "很爱", "好物", "好用"}
_NEGATIVE_WORDS = {"很差", "太烂", "太坑", "难吃", "难喝", "失望", "垃圾", "骗子", "不好",
                   "吐槽", "踩雷", "不推荐", "差评", "翻车", "无语", "恶心", "后悔",
                   "太差", "不好用", "不靠谱", "很差劲", "难看", "难用"}


def _simple_sentiment(text: str) -> float:
    """基于词库的简单情感评分（0-1），snownlp 不可用时使用。"""
    score = 0.5
    for w in _POSITIVE_WORDS:
        if w in text:
            score += 0.08
    for w in _NEGATIVE_WORDS:
        if w in text:
            score -= 0.08
    return max(0.0, min(1.0, score))


def _score_sentiment(text: str) -> float:
    """返回 0-1 情感分数。"""
    if not text or not text.strip():
        return 0.5
    if _has_snownlp:
        try:
            return SnowNLP(text).sentiments
        except Exception:
            pass
    return _simple_sentiment(text)


def _sentiment_label(score: float) -> str:
    if score < 0.4:
        return "负面"
    if score > 0.7:
        return "正面"
    return "中性"


def analyze_sentiment(
    conn,
    note_id: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """对评论做情感分析。返回统计摘要。"""
    if not _has_snownlp:
        print("[ANALYZE] snownlp 未安装，使用简单词库分析（精度较低）。pip install snownlp",
              file=sys.stderr)

    # 收集评论
    if note_id:
        comments = list(xhs_storage.iter_comments(conn, note_id))
    elif keyword:
        # 通过 search_cache 找 note_ids，再找评论
        comments = _comments_by_keyword(conn, keyword)
    else:
        # 全部评论
        comments = _all_comments(conn)

    if not comments:
        return {"total": 0, "msg": "无评论数据"}

    # 评分
    scores: list[tuple[dict, float, str]] = []
    for c in comments:
        text = c["content"] or ""
        score = _score_sentiment(text)
        label = _sentiment_label(score)
        scores.append((dict(c), score, label))

    # 统计
    total = len(scores)
    dist = Counter(label for _, _, label in scores)
    avg_score = sum(s for _, s, _ in scores) / total if total else 0.5

    # 代表性评论（每档取最极端的 3 条）
    positive = sorted([s for s in scores if s[2] == "正面"], key=lambda x: x[1], reverse=True)
    negative = sorted([s for s in scores if s[2] == "负面"], key=lambda x: x[1])
    neutral  = [s for s in scores if s[2] == "中性"]

    return {
        "total": total,
        "average_score": round(avg_score, 3),
        "distribution": {
            "正面": dist.get("正面", 0),
            "中性": dist.get("中性", 0),
            "负面": dist.get("负面", 0),
        },
        "distribution_pct": {
            "正面": round(dist.get("正面", 0) / total * 100, 1) if total else 0,
            "中性": round(dist.get("中性", 0) / total * 100, 1) if total else 0,
            "负面": round(dist.get("负面", 0) / total * 100, 1) if total else 0,
        },
        "top_positive": [{"nickname": c["nickname"], "content": c["content"][:60], "score": round(s, 2)}
                         for c, s, _ in positive[:3]],
        "top_negative": [{"nickname": c["nickname"], "content": c["content"][:60], "score": round(s, 2)}
                         for c, s, _ in negative[:3]],
        "engine": "snownlp" if _has_snownlp else "simple-lexicon",
    }


# ---------------------------------------------------------------------------
# 话题聚类
# ---------------------------------------------------------------------------

def _extract_keywords(text: str, top_k: int = 5) -> list[str]:
    """提取关键词。jieba 可用时用 TF-IDF，否则基于中文词频统计。"""
    if not text or not text.strip():
        return []
    if _has_jieba:
        try:
            return jieba.analyse.extract_tags(text, topK=top_k)
        except Exception:
            pass
    # 降级方案：提取 2-4 字中文词组，按词频取 top_k
    import re
    from collections import Counter
    # 匹配 2-4 字连续中文
    words = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
    # 过滤停用词
    stopwords = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都',
                 '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会',
                 '着', '没有', '看', '好', '自己', '这', '他', '她', '它', '什么'}
    words = [w for w in words if w not in stopwords]
    return [w for w, _ in Counter(words).most_common(top_k)]


def analyze_topics(
    conn,
    keyword: str | None = None,
    user_id: str | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """话题聚类分析。返回热门话题、关键词频率、内容类型分布。"""
    if not _has_jieba:
        print("[ANALYZE] jieba 未安装，关键词提取精度较低。pip install jieba",
              file=sys.stderr)

    notes = list(xhs_storage.iter_notes(conn, user_id=user_id))

    # 按关键词过滤
    if keyword:
        notes = [n for n in notes if keyword in (n["title"] or "") or keyword in (n["description"] or "")]

    if not notes:
        return {"total": 0, "msg": "无笔记数据"}

    total = len(notes)

    # 1. 话题标签统计
    tag_counter: Counter[str] = Counter()
    for n in notes:
        topics = json.loads(n["topics"] or "[]")
        for t in topics:
            if t:
                tag_counter[t] += 1

    # 2. 关键词频率（jieba TF-IDF）
    kw_counter: Counter[str] = Counter()
    for n in notes:
        text = f"{n['title'] or ''} {n['description'] or ''}"
        for kw in _extract_keywords(text, top_k=3):
            kw_counter[kw] += 1

    # 3. 内容类型分布
    type_counter: Counter[str] = Counter()
    for n in notes:
        note_type = n["type"] or "unknown"
        type_counter[note_type] += 1

    # 4. IP 属地分布
    ip_counter: Counter[str] = Counter()
    for n in notes:
        ip = n["ip_location"] or ""
        if ip:
            ip_counter[ip] += 1

    # 5. 互动数据汇总
    total_likes = sum(n["liked_count"] or 0 for n in notes)
    total_collects = sum(n["collected_count"] or 0 for n in notes)
    total_comments = sum(n["comment_count"] or 0 for n in notes)

    return {
        "total": total,
        "tags": tag_counter.most_common(top_n),
        "keywords": kw_counter.most_common(top_n),
        "types": dict(type_counter),
        "ip_locations": ip_counter.most_common(10),
        "engagement": {
            "total_likes": total_likes,
            "total_collects": total_collects,
            "total_comments": total_comments,
            "avg_likes": round(total_likes / total, 1) if total else 0,
        },
        "engine": "jieba" if _has_jieba else "simple",
    }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _comments_by_keyword(conn, keyword: str) -> list:
    """通过 search_cache → note_ids → comments 路径找评论。"""
    cur = conn.execute("SELECT note_ids_json FROM search_cache WHERE keyword = ?", (keyword,))
    rows = cur.fetchall()
    comments = []
    seen_notes: set[str] = set()
    for row in rows:
        note_ids = json.loads(row["note_ids_json"] or "[]")
        for nid in note_ids:
            if nid in seen_notes:
                continue
            seen_notes.add(nid)
            comments.extend(xhs_storage.iter_comments(conn, nid))
    return comments


def _all_comments(conn) -> list:
    """全部评论（按 note_id 遍历）。"""
    cur = conn.execute("SELECT DISTINCT note_id FROM comments")
    note_ids = [r["note_id"] for r in cur.fetchall()]
    comments = []
    for nid in note_ids:
        comments.extend(xhs_storage.iter_comments(conn, nid))
    return comments


# ---------------------------------------------------------------------------
# 文本报告输出
# ---------------------------------------------------------------------------

def print_sentiment_report(result: dict[str, Any]) -> None:
    if result.get("total", 0) == 0:
        print(result.get("msg", "无数据"))
        return
    print(f"=== 情感分析报告（{result['total']} 条评论，引擎: {result['engine']}）===")
    print(f"  平均情感分: {result['average_score']}")
    print(f"  分布: 正面 {result['distribution']['正面']} ({result['distribution_pct']['正面']}%)"
          f" | 中性 {result['distribution']['中性']} ({result['distribution_pct']['中性']}%)"
          f" | 负面 {result['distribution']['负面']} ({result['distribution_pct']['负面']}%)")
    if result.get("top_positive"):
        print("  代表性正面评论:")
        for c in result["top_positive"]:
            print(f"    [{c['nickname']}] {c['content']} (score={c['score']})")
    if result.get("top_negative"):
        print("  代表性负面评论:")
        for c in result["top_negative"]:
            print(f"    [{c['nickname']}] {c['content']} (score={c['score']})")


def print_topics_report(result: dict[str, Any]) -> None:
    if result.get("total", 0) == 0:
        print(result.get("msg", "无数据"))
        return
    print(f"=== 话题聚类报告（{result['total']} 条笔记，引擎: {result['engine']}）===")
    if result["tags"]:
        print("  热门话题:")
        for tag, count in result["tags"]:
            print(f"    {tag}: {count}")
    if result["keywords"]:
        print("  高频关键词:")
        for kw, count in result["keywords"]:
            print(f"    {kw}: {count}")
    if result["types"]:
        print(f"  内容类型: {result['types']}")
    if result["ip_locations"]:
        print("  IP 属地分布:")
        for ip, count in result["ip_locations"]:
            print(f"    {ip}: {count}")
    eng = result.get("engagement", {})
    if eng:
        print(f"  互动汇总: 赞 {eng['total_likes']} | 藏 {eng['total_collects']}"
              f" | 评 {eng['total_comments']} | 平均赞 {eng['avg_likes']}")


# ---------------------------------------------------------------------------
# CLI command handler
# ---------------------------------------------------------------------------

def cmd_analyze(args) -> int:
    """评论情感分析 & 话题聚类 CLI 入口。"""
    import json
    import xhs_storage

    conn = xhs_storage.connect()
    try:
        analyze_type = getattr(args, "type", "topics")
        output = getattr(args, "output", "text")

        if analyze_type == "sentiment":
            result = analyze_sentiment(
                conn,
                note_id=getattr(args, "note", None),
                keyword=getattr(args, "keyword", None),
            )
            if output == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print_sentiment_report(result)
        else:
            result = analyze_topics(
                conn,
                keyword=getattr(args, "keyword", None),
                user_id=getattr(args, "user", None),
                top_n=20,
            )
            if output == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print_topics_report(result)
        return 0
    finally:
        conn.close()
