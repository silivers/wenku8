"""集中管理磁盘资源路径，避免散落硬编码。

爬虫与导出器都从这里取路径，将来切换存储后端只需改这一处。
路径相对当前工作目录（cwd），与 config 中 SQLite 默认路径保持一致。
"""

from pathlib import Path

DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"  # data/images/{novel_id}/{chapter_id}/...
COVERS_DIR = DATA_DIR / "covers"  # data/covers/{novel_id}.jpg
