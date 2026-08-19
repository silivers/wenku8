-- 003_site_meta.sql — 站点级全站遍历精确数量（最后一个有效小说编号）

CREATE TABLE IF NOT EXISTS site_meta (
    site           TEXT PRIMARY KEY,
    last_novel_id  INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT
);
