"""
过滤 LLM 流式输出中的思考过程，仅保留最终回答文本。
"""
from __future__ import annotations

import re

_THINK_TAG_OPEN = re.compile(r"<\s*think\s*>", re.IGNORECASE)
_THINK_TAG_CLOSE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)
_THINKING_PROCESS = re.compile(r"ThinkingProcess\s*:", re.IGNORECASE)

_THINK_BLOCK_RE = re.compile(
    r"<\s*think\s*>.*?<\s*/\s*think\s*>",
    re.DOTALL | re.IGNORECASE,
)
_THINKING_PROCESS_BLOCK_RE = re.compile(
    r"ThinkingProcess\s*:.*?(?=\n\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_T_OPEN = "<" + "think" + ">"
_T_CLOSE = "</" + "think" + ">"
_PARTIAL_MARKERS = (
    _T_OPEN,
    _T_CLOSE,
    "ThinkingProcess",
    "ThinkingProces",
    "ThinkingProc",
)


def strip_thinking_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINKING_PROCESS_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


class ThinkingStreamFilter:
    """流式增量过滤：抑制 <think> 与 ThinkingProcess 前缀段。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think_tag = False
        self._in_thinking_process = False

    def feed(self, piece: str) -> str:
        if not piece:
            return ""
        self._buffer += piece
        return self._drain(safe_end=False)

    def flush(self) -> str:
        if self._in_think_tag or self._in_thinking_process:
            self._buffer = ""
            self._in_think_tag = False
            self._in_thinking_process = False
            return ""
        tail = self._drain(safe_end=True)
        self._buffer = ""
        return strip_thinking_text(tail)

    def _drain(self, *, safe_end: bool) -> str:
        emitted: list[str] = []

        while self._buffer:
            if self._in_think_tag:
                close_match = _THINK_TAG_CLOSE.search(self._buffer)
                if not close_match:
                    self._buffer = ""
                    break
                self._buffer = self._buffer[close_match.end():]
                self._in_think_tag = False
                continue

            if self._in_thinking_process:
                split_at = self._buffer.find("\n\n")
                if split_at == -1:
                    if safe_end:
                        self._buffer = ""
                    break
                self._buffer = self._buffer[split_at + 2:]
                self._in_thinking_process = False
                continue

            tag_match = _THINK_TAG_OPEN.search(self._buffer)
            tp_match = _THINKING_PROCESS.search(self._buffer)
            next_special = None
            if tag_match and tp_match:
                next_special = tag_match if tag_match.start() <= tp_match.start() else tp_match
            elif tag_match:
                next_special = tag_match
            elif tp_match:
                next_special = tp_match

            if next_special and next_special.start() > 0:
                emitted.append(self._buffer[: next_special.start()])
                self._buffer = self._buffer[next_special.start():]
                continue

            if tag_match and tag_match.start() == 0:
                self._buffer = self._buffer[tag_match.end():]
                self._in_think_tag = True
                continue

            if tp_match and tp_match.start() == 0:
                self._buffer = self._buffer[tp_match.end():]
                self._in_thinking_process = True
                continue

            if safe_end:
                emitted.append(self._buffer)
                self._buffer = ""
                break

            keep_from = self._safe_emit_upto()
            if keep_from <= 0:
                break
            emitted.append(self._buffer[:keep_from])
            self._buffer = self._buffer[keep_from:]

        return "".join(emitted)

    def _safe_emit_upto(self) -> int:
        limit = len(self._buffer)
        for marker in _PARTIAL_MARKERS:
            idx = self._buffer.find(marker)
            if idx != -1:
                limit = min(limit, idx)
        return limit
