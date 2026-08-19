"""乱码检测"""

from __future__ import annotations

import re


class MojibakeDetector:
    """检测文本中可能的编码损坏（乱码）。"""

    MOJIBAKE_PATTERNS = [
        r"[\ufffd]{3,}",          # Unicode 替换字符连续出现
        r"[\u00c0-\u00ff]{8,}",   # Latin-1 补充区连续出现
        r"\xc3[\x80-\xbf]",       # UTF-8 被当作 Latin-1 的典型模式
        r"\xe4[\x00-\xff]{2}",    # 不完整的 UTF-8 序列
    ]

    def detect(self, text: str) -> bool:
        """返回 True 表示检测到乱码。"""
        if not text:
            return False

        for pattern in self.MOJIBAKE_PATTERNS:
            if re.search(pattern, text):
                return True

        # 替换字符比例检测
        replace_count = text.count("\ufffd")
        if replace_count / max(len(text), 1) > 0.01:
            return True

        return False
