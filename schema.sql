-- 小红书数据表
-- 对应 xhs_storage.py 中的 SQLite 表结构
-- v2: 增加 image_urls / video_url 提取、content 正文 fallback、note_url
--
-- 【列名映射说明】
-- SQLite               → PG (本文件)          说明
-- raw_json (TEXT)      → raw_data (JSONB)     hub_adapter._sqlite_json() 自动转换
-- pictures_json (TEXT) → pictures (JSONB)     同上
-- note_ids_json (TEXT) → note_ids (JSONB)     同上
-- content (TEXT)       → content (TEXT)       v2 新增，与 SQLite 统一
-- note_url (TEXT)      → note_url (TEXT)      v2 新增，与 SQLite 统一

CREATE TABLE IF NOT EXISTS xhs_notes (
    note_id TEXT PRIMARY KEY,
    user_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    type TEXT DEFAULT '',
    liked_count INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    topics JSONB DEFAULT '[]',
    published_at TEXT DEFAULT '',
    xsec_token TEXT DEFAULT '',
    xsec_source TEXT DEFAULT '',
    -- 视频相关
    video_url TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    video_duration INTEGER DEFAULT 0,
    video_transcript TEXT DEFAULT '',
    video_ocr_text TEXT DEFAULT '',
    video_summary TEXT DEFAULT '',
    -- 图片相关
    image_ocr_text TEXT DEFAULT '',
    image_summary TEXT DEFAULT '',
    image_mermaid TEXT DEFAULT '',
    image_urls JSONB DEFAULT '[]',
    -- v2 新增：从 raw_data 提取的增强字段
    content TEXT DEFAULT '',                -- 正文（从 raw_data.desc 提取，比 description 更完整）
    note_url TEXT DEFAULT '',               -- 笔记原始链接
    -- v2 新增：本地文件路径
    media_path TEXT DEFAULT '',             -- 本地媒体目录相对路径（data/media/<博主>/<笔记>/）
    has_local_media BOOLEAN DEFAULT FALSE, -- 是否有本地下载的图片/视频
    -- 通用
    content_hash TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    avatar TEXT DEFAULT '',
    description TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    follow_count INTEGER DEFAULT 0,
    notes_count INTEGER DEFAULT 0,
    location TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_comments (
    comment_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL DEFAULT '',
    parent_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    content TEXT DEFAULT '',
    like_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    pictures JSONB DEFAULT '[]',
    target_comment_id TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_search_cache (
    keyword TEXT NOT NULL,
    page INTEGER NOT NULL,
    note_ids JSONB DEFAULT '[]',
    crawled_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (keyword, page)
);

CREATE TABLE IF NOT EXISTS xhs_crawl_state (
    task_id TEXT PRIMARY KEY,
    task_type TEXT DEFAULT '',
    target_id TEXT DEFAULT '',
    cursor TEXT DEFAULT '',
    status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_xhs_notes_user ON xhs_notes(user_id);
CREATE INDEX IF NOT EXISTS idx_xhs_notes_crawled ON xhs_notes(crawled_at);
CREATE INDEX IF NOT EXISTS idx_xhs_comments_note ON xhs_comments(note_id);
CREATE INDEX IF NOT EXISTS idx_xhs_comments_parent ON xhs_comments(parent_id);

-- v3: 对齐 substack 模板，补齐 status / updated_at（幂等迁移已存在的旧表）
ALTER TABLE xhs_notes    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_notes    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE xhs_users    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_users    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE xhs_comments ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_comments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
