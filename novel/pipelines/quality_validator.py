"""质量校验管道"""

from __future__ import annotations

from loguru import logger

from novel.core.models import ChapterContent
from novel.core.pipeline import Pipeline, PipelineContext
from novel.utils.mojibake import MojibakeDetector
from novel.utils.text import appears_truncated


class QualityValidatorPipeline(Pipeline):
    """内容质量校验：发现问题记日志，不阻断流程。

    不做字数检查 —— 引言、后记、插图页等章节本身就很短，不应视为异常。
    """

    def __init__(
        self,
        honeypot_patterns: list[str] | None = None,
        drop_on_error: bool = False,
        **_kwargs,
    ):
        self.honeypot_patterns = honeypot_patterns or [
            "请刷新页面重试",
            "检测到异常访问",
        ]
        self.drop_on_error = drop_on_error
        self._mojibake_detector = MojibakeDetector()

    async def process_item(
        self, item: ChapterContent, context: PipelineContext
    ) -> ChapterContent | None:
        issues: list[tuple[str, str]] = []

        # 1. 乱码检测
        if self._mojibake_detector.detect(item.content):
            issues.append(("error", "疑似编码损坏（乱码）"))

        # 2. 反爬蜜罐检测
        for pattern in self.honeypot_patterns:
            if pattern in item.content:
                issues.append(("error", f"蜜罐模式匹配: {pattern}"))
                break

        # 3. 内容截断检测
        if appears_truncated(item.content):
            issues.append(("warning", "内容可能被截断"))

        # 记录问题
        has_error = False
        for level, msg in issues:
            getattr(logger, level)(f"[{item.chapter_id}] {msg}")
            if level == "error":
                has_error = True

        # 错误时可选择丢弃
        if has_error and self.drop_on_error:
            logger.warning(f"章节因质量错误被丢弃: {item.chapter_id}")
            return None

        return item  # 默认始终返回
