-- 002_add_images.sql — 给 chapters 表添加 images 列

ALTER TABLE chapters ADD COLUMN images TEXT DEFAULT '[]';
