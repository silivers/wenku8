-- 004_image_crawl.sql — 图片独立爬取：书籍状态 + 图片任务表
--
-- 目标：正文与图片爬取解耦（图片失败可轮询重试直至全部成功 / 下架书跳过）。
-- 1. novels.book_status：active=正常 / takedown=下架(版权删除) / deleted=已删除
-- 2. images 表替代 resources：承载下载状态机
--    status: pending -> downloaded | failed ->(下一轮) downloaded
--                                    failed ->(attempt>=max_rounds) skipped
-- 3. 存量迁移：chapters.images JSON 与 novels.cover_url 全部按 pending 重新登记
--    （resources.chapter_id 恒为 NULL、双轨存储不可信，不做回填；图片是 CDN
--    静态资源，重下成本远低于修脏数据）
-- 4. 退役 resources 表与 chapters.images 列（DCOL 需 SQLite>=3.35，随 Python 满足）

-- 1. 书籍状态
ALTER TABLE novels ADD COLUMN book_status TEXT NOT NULL DEFAULT 'active';

-- 2. 图片任务表
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id TEXT NOT NULL,
    chapter_site_id TEXT,
    resource_type TEXT NOT NULL DEFAULT 'illustration',
    remote_url TEXT NOT NULL,
    local_path TEXT,
    file_hash TEXT,
    image_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(novel_id, remote_url),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
CREATE INDEX IF NOT EXISTS idx_images_novel_status ON images(novel_id, status);

-- 3. 存量迁移：chapters.images JSON -> pending 行
INSERT OR IGNORE INTO images
    (novel_id, chapter_site_id, resource_type, remote_url, image_order, status)
SELECT c.novel_id, c.site_chapter_id, 'illustration',
       json_extract(j.value, '$.url'),
       COALESCE(json_extract(j.value, '$.index'), 0),
       'pending'
FROM chapters c, json_each(c.images) j
WHERE c.images IS NOT NULL
  AND json_extract(j.value, '$.url') IS NOT NULL
  AND json_extract(j.value, '$.url') != '';

-- 4. 存量封面也登记为 pending
INSERT OR IGNORE INTO images
    (novel_id, chapter_site_id, resource_type, remote_url, status)
SELECT id, NULL, 'cover', cover_url, 'pending'
FROM novels
WHERE cover_url IS NOT NULL AND cover_url != '';

-- 5. 旧表/旧列退役
DROP TABLE IF EXISTS resources;
ALTER TABLE chapters DROP COLUMN images;
