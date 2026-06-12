---
name: xiaohongshu-query
description: Query Xiaohongshu notes, users, and comments stored in the local PostgreSQL database. Read-only — no data modification.
metadata:
  openclaw:
    requires:
      bins: ["python3"]
---

# 小红书数据库查询 Skill

本 skill 查询已爬取并存储在 PostgreSQL 数据库中的小红书内容（笔记、用户、评论）。

**本 skill 严格只读。不执行任何 INSERT, UPDATE, DELETE 操作。**

## 适用场景

用于以下情况：
- 从数据库中读取或搜索小红书笔记、用户、评论
- 按关键词、用户ID、类型或日期范围查找笔记
- 查看某篇笔记的完整正文
- 获取已爬取内容的统计信息

**不要**用于爬取/下载新的小红书内容（那是不同的工作流）。

## 命令

所有命令使用相同的基础调用：

```bash
python {baseDir}/scripts/query_db.py <command> [options]
```

### 1. 查询笔记

```bash
python {baseDir}/scripts/query_db.py notes [options]
```

选项：
- `--user USER_ID` — 按用户ID过滤
- `--type TYPE` — 按类型过滤：`图文`, `video`
- `--search KEYWORD` — 搜索标题和正文（不区分大小写）
- `--since YYYY-MM-DD` — 起始日期 (含)
- `--until YYYY-MM-DD` — 截止日期 (含)
- `--limit N` — 最多返回条数 (默认: 20, 最大: 500)
- `--offset N` — 跳过前 N 条 (分页)
- `--id NOTE_ID` — 按笔记ID精确查询单条
- `--full` — 显示完整正文 (默认只显示预览)

### 2. 列出用户

```bash
python {baseDir}/scripts/query_db.py users [--search KEYWORD]
```

返回所有已爬取的用户，按粉丝数降序。

### 3. 查看统计

```bash
python {baseDir}/scripts/query_db.py stats
```

返回笔记总数、用户总数、评论总数，以及按类型分类统计。

## 输出格式

每条笔记以稳定的结构化格式输出：

```
标题: <title>
来源: xiaohongshu
类型: <图文|video>
作者ID: <user_id>
发布时间: <datetime>
原始链接: <url>
IP属地: <location>
点赞: N  收藏: N  评论: N
话题: <topic1>, <topic2>
正文预览: <first 200 chars>  (默认)
--- 正文 ---              (带 --full 参数)
<full body text>
```

条目之间用 `============` 分隔线分隔。

## 示例

用户说: "数据库里有哪些小红书笔记"
→ 运行: `python {baseDir}/scripts/query_db.py notes --limit 10`

用户说: "搜索小红书里关于AI的笔记"
→ 运行: `python {baseDir}/scripts/query_db.py notes --search AI`

用户说: "查看用户 abc123 的笔记"
→ 运行: `python {baseDir}/scripts/query_db.py notes --user abc123 --limit 20`

用户说: "看看笔记ID为 xyz789 的完整内容"
→ 运行: `python {baseDir}/scripts/query_db.py notes --id xyz789 --full`

用户说: "2025年1月的所有视频笔记"
→ 运行: `python {baseDir}/scripts/query_db.py notes --type video --since 2025-01-01 --until 2025-01-31`

用户说: "小红书数据库里有多少数据"
→ 运行: `python {baseDir}/scripts/query_db.py stats`

## 配置

本 skill 通过 `{baseDir}/.env` 配置数据库连接，使用 `POSTGRES_READONLY_USER` (hub_readonly) 只读用户。
