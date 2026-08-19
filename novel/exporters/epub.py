"""EPUB 导出器"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from loguru import logger

from novel.core.paths import COVERS_DIR, IMAGES_DIR
from novel.core.storage import StorageBackend


class EPUBExporter:
    """EPUB 3.0 导出器。

    从存储层读取已爬取的小说数据，生成 EPUB 文件。
    支持封面、卷分隔、章节插图嵌入、作者后记。
    """

    def __init__(
        self,
        storage: StorageBackend,
        output_dir: str = "./output",
        font_family: str = "Georgia, Noto Serif SC",
        base_font_size: str = "14pt",
        line_height: float = 1.8,
        paragraph_indent: str = "2em",
        split_by_volume: bool = True,
    ):
        self.storage = storage
        self.output_dir = output_dir
        self.font_family = font_family
        self.base_font_size = base_font_size
        self.line_height = line_height
        self.paragraph_indent = paragraph_indent
        self.split_by_volume = split_by_volume

    async def export(self, novel_internal_id: str, output_path: str | None = None) -> str:
        """导出 EPUB，返回文件路径。"""
        from ebooklib import epub

        novel = await self.storage.get_novel_by_internal_id(novel_internal_id)
        if not novel:
            raise ValueError(f"Novel not found: {novel_internal_id}")

        chapters = await self.storage.get_chapters(novel_internal_id)
        if not chapters:
            raise ValueError(f"No chapters found for novel: {novel.title}")

        logger.info(f"开始导出 EPUB: {novel.title} ({len(chapters)} 章)")

        book = epub.EpubBook()
        book.set_identifier(f"novel-crawler-{novel_internal_id}")
        book.set_title(novel.title)
        book.set_language("zh")
        book.add_author(novel.author or "Unknown")

        if novel.description:
            book.add_metadata("DC", "description", novel.description)
        if novel.tags:
            book.add_metadata("DC", "subject", ", ".join(novel.tags))

        book.add_metadata(
            None, "meta", "",
            {"name": "source", "content": f"site={novel.extra.get('site', '')}"},
        )

        # CSS
        css_content = self._load_css()
        style_item = epub.EpubItem(
            uid="style",
            file_name="style/default.css",
            media_type="text/css",
            content=css_content,
        )
        book.add_item(style_item)

        # 封面
        cover_path = self._get_cover_path(novel_internal_id)
        if cover_path and Path(cover_path).exists():
            with open(cover_path, "rb") as f:
                cover_data = f.read()
            book.set_cover("cover.jpg", cover_data)

        # 目录页
        toc_chapter = epub.EpubHtml(
            title="目录",
            file_name="toc.xhtml",
            lang="zh",
        )
        toc_chapter.content = '<h1>目录</h1>'
        toc_chapter.add_item(style_item)
        book.add_item(toc_chapter)

        # 收集所有需要嵌入的图片
        embedded_images: dict[str, str] = {}  # src_path -> media_type

        # 章节内容
        epub_chapters: list[Any] = []
        current_volume: str | None = None

        for i, ch in enumerate(chapters):
            volume = ch.get("volume")

            # 卷分隔
            if self.split_by_volume and volume and volume != current_volume:
                current_volume = volume
                vol_chapter = epub.EpubHtml(
                    title=volume,
                    file_name=f"volume_{len(epub_chapters)}.xhtml",
                    lang="zh",
                )
                vol_chapter.content = f'<h1 class="volume-title">{volume}</h1>'
                vol_chapter.add_item(style_item)
                book.add_item(vol_chapter)
                epub_chapters.append(vol_chapter)

            ch_title = ch.get("title", f"第{i+1}章")
            content = ch.get("content", "")
            content_format = ch.get("content_format", "html")

            # 转换为 XHTML
            if content_format == "html":
                body_html = self._html_to_xhtml_body(content)
            else:
                body_html = "".join(
                    f"<p>{line}</p>" for line in content.split("\n") if line.strip()
                )

            # 扫描并嵌入章节插图
            body_html, ch_images = self._embed_images(
                body_html, novel_internal_id, ch.get("site_chapter_id", str(i))
            )
            for img_path, media_type in ch_images:
                embedded_images[img_path] = media_type

            # 作者后记
            author_note = ch.get("author_note")
            if author_note:
                body_html += (
                    f'<div class="author-note">'
                    f'<div class="author-note-title">后记</div>'
                    f'<p>{author_note}</p>'
                    f'</div>'
                )

            chapter_item = epub.EpubHtml(
                title=ch_title,
                file_name=f"chapter_{i+1}.xhtml",
                lang="zh",
            )
            chapter_item.content = (
                f'<html><head><title>{ch_title}</title></head><body>'
                f'<h1 class="chapter-title">{ch_title}</h1>'
                f'{body_html}'
                f'</body></html>'
            )
            chapter_item.add_item(style_item)
            book.add_item(chapter_item)
            epub_chapters.append(chapter_item)

        # 将所有图片作为 EpubItem 嵌入
        for i, (img_path, media_type) in enumerate(embedded_images.items()):
            # img_path 形如 images/{chapter_id}/{filename}
            local_rel = img_path[len("images/"):] if img_path.startswith("images/") else img_path
            full_path = IMAGES_DIR / novel_internal_id / local_rel
            if full_path.exists():
                with open(full_path, "rb") as f:
                    img_data = f.read()
                img_item = epub.EpubItem(
                    uid=f"img_{i}",
                    file_name=img_path,
                    media_type=media_type,
                    content=img_data,
                )
                book.add_item(img_item)
            else:
                logger.warning(f"插图文件不存在: {full_path}")

        if embedded_images:
            logger.info(f"嵌入 {len(embedded_images)} 张插图")

        # 目录
        book.toc = epub_chapters

        # 导航
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # 阅读顺序
        book.spine = [toc_chapter] + epub_chapters

        # 写入文件
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        if output_path is None:
            from novel.utils.text import safe_filename

            filename = f"{safe_filename(novel.title)}.epub"
            output_path = str(Path(self.output_dir) / filename)

        epub.write_epub(output_path, book, {})
        logger.info(f"EPUB 导出完成: {output_path}")

        return output_path

    def _embed_images(
        self, html: str, novel_id: str, chapter_id: str
    ) -> tuple[str, list[tuple[str, str]]]:
        """扫描 HTML 中的图片引用，返回 (修正后的 HTML, [(epub_path, media_type), ...])。

        爬虫已将远程 URL 替换为 images/{chapter_id}/xxx 形式，
        此方法验证文件是否存在，确保 EPUB 内部路径正确。
        """
        images: list[tuple[str, str]] = []
        pattern = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>', re.IGNORECASE)

        def replace_img(match: re.Match) -> str:
            src = match.group(1)
            # 只处理本地 images/ 开头的路径
            if src.startswith("images/"):
                # src 形如 images/{chapter_id}/{filename}
                # 本地文件在 data/images/{novel_id}/{chapter_id}/{filename}
                local_rel = src[len("images/"):]  # 去掉 images/ 前缀
                full_path = IMAGES_DIR / novel_id / local_rel
                if full_path.exists():
                    media_type = self._get_media_type(full_path.suffix)
                    images.append((src, media_type))
                    return match.group(0)
                else:
                    logger.debug(f"插图文件不存在，跳过: {full_path}")
                    return ""
            return match.group(0)

        result = pattern.sub(replace_img, html)
        return result, images

    @staticmethod
    def _get_media_type(ext: str) -> str:
        ext = ext.lower()
        mapping = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        return mapping.get(ext, "image/jpeg")

    def _load_css(self) -> str:
        css_path = Path(__file__).parent / "templates" / "epub" / "style.css"
        if css_path.exists():
            return css_path.read_text("utf-8")
        return ""

    def _get_cover_path(self, novel_internal_id: str) -> str | None:
        cover_path = str(COVERS_DIR / f"{novel_internal_id}.jpg")
        if Path(cover_path).exists():
            return cover_path
        return None

    def _html_to_xhtml_body(self, html_content: str) -> str:
        from lxml import html as lxml_html

        try:
            tree = lxml_html.fromstring(f"<div>{html_content}</div>")
            inner_html = lxml_html.tostring(tree, encoding="unicode")
            if inner_html.startswith("<div>") and inner_html.endswith("</div>"):
                inner_html = inner_html[5:-6]
            return inner_html
        except Exception:
            lines = html_content.split("\n")
            return "".join(
                f"<p>{line}</p>" for line in lines if line.strip()
            )
