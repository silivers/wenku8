-- 001_initial.sql — 初始表结构

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS novels (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    site_novel_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    description TEXT,
    cover_url TEXT,
    cover_local TEXT,
    status TEXT,
    categories TEXT,   -- JSON array
    tags TEXT,          -- JSON array
    word_count INTEGER DEFAULT 0,
    last_updated TEXT,
    crawl_time TEXT,
    extra TEXT,         -- JSON object
    UNIQUE(site, site_novel_id)
);

CREATE TABLE IF NOT EXISTS volumes (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    name TEXT,
    idx REAL DEFAULT 0,
    cover_url TEXT,
    cover_local TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    volume_id TEXT,
    site_chapter_id TEXT,
    title TEXT NOT NULL,
    chapter_index REAL DEFAULT 0,
    content TEXT,
    content_format TEXT DEFAULT 'html',
    word_count INTEGER DEFAULT 0,
    author_note TEXT,
    is_crawled INTEGER DEFAULT 0,
    crawl_time TEXT,
    content_hash TEXT,
    volume TEXT,
    UNIQUE(novel_id, site_chapter_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (volume_id) REFERENCES volumes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_id TEXT,
    resource_type TEXT,
    remote_url TEXT,
    local_path TEXT,
    caption TEXT,
    file_hash TEXT,
    image_order INTEGER DEFAULT 0,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_states (
    task_id TEXT PRIMARY KEY,
    novel_id TEXT,
    site TEXT,
    status TEXT,
    total_chapters INTEGER DEFAULT 0,
    crawled_chapters INTEGER DEFAULT 0,
    failed_chapters TEXT,     -- JSON array
    last_chapter_id TEXT,
    started_at TEXT,
    updated_at TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_chapters_novel ON chapters(novel_id);
CREATE INDEX IF NOT EXISTS idx_chapters_site ON chapters(site_chapter_id, novel_id);
CREATE INDEX IF NOT EXISTS idx_resources_novel ON resources(novel_id);
CREATE INDEX IF NOT EXISTS idx_resources_chapter ON resources(chapter_id);
CREATE INDEX IF NOT EXISTS idx_novels_site ON novels(site);
