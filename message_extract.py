"""从 Dify 传入的消息、工具定义与模型参数中提取可检查文本，并统计非文本段数量。

设计约束：
  - 不打印/记录任何内容，只做内存内提取；
  - 非文本段（图片/音频/视频/文件等）计数，供未知内容策略使用；
  - 文本段同时取 SDK 的 TextPromptMessageContent.data 与兼容字段 .text。
"""

from __future__ import annotations

import json
import re

# 保留语法：[routing:XX] 或 [路由：XX]（冒号可半角/全角，大小写不敏感）。
# 应用开发者在提示词里放这个标记（常配合变量引用 {{#节点.输出#}} 由 Dify 渲染），
# 捕获值作为独立行参与关键词匹配；转发上游前由 targets 剔除，模型看不到。
# 仅喂关键词、不支持直接指定路由名——最终用户不能自行指定目标。
MARKER_RE = re.compile(r"\[(?:routing|路由)\s*[:：]\s*([^\]\n]{0,64})\]", re.IGNORECASE)


def strip_markers(text: str) -> str:
    """从将发给上游的文本中剔除路由标记：独占一行的标记整行删除，
    行内混排的只摘掉标记本身；不改动与标记无关的行。"""
    if not text or "[" not in text or not MARKER_RE.search(text):
        return text
    out = []
    for line in text.split("\n"):
        cleaned = MARKER_RE.sub("", line)
        if cleaned != line and cleaned.strip() == "":
            continue  # 整行都是标记 → 删除该行
        out.append(cleaned)
    return "\n".join(out)


def _part_text(part) -> str | None:
    """可作文本检查的段返回其文本，否则 None（视为非文本/未知段）。"""
    if isinstance(part, str):
        return part
    ptype = getattr(part, "type", None)
    ptype = getattr(ptype, "value", ptype)
    if ptype == "text":
        text = getattr(part, "data", None)
        if not isinstance(text, str):
            text = getattr(part, "text", None)
        return text if isinstance(text, str) else ""
    return None


def _content_text_and_unknown(content) -> tuple:
    """返回 (拼接文本, 非文本段数量)。"""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    texts = []
    unknown = 0
    for part in content:
        text = _part_text(part)
        if text is None:
            unknown += 1
        else:
            texts.append(text)
    return "".join(texts), unknown


def extract_text(prompt_messages, tools, model_parameters) -> tuple:
    """返回 (可检查文本, 非文本段数量)。文本用于关键词匹配，不用于日志。"""
    chunks = []
    unknown = 0

    for pm in prompt_messages or []:
        text, u = _content_text_and_unknown(getattr(pm, "content", None))
        unknown += u
        if text:
            chunks.append(text)
        for tc in getattr(pm, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            if fn is not None:
                chunks.append(f"{getattr(fn, 'name', '')} {getattr(fn, 'arguments', '')}")

    for tool in tools or []:
        chunks.append(str(getattr(tool, "name", "") or ""))
        chunks.append(str(getattr(tool, "description", "") or ""))
        params = getattr(tool, "parameters", None)
        if params:
            chunks.append(json.dumps(params, ensure_ascii=False, default=str))

    for value in (model_parameters or {}).values():
        if isinstance(value, str):
            chunks.append(value)

    # [routing:XX] 保留语法：捕获值作为独立行参与匹配（与正文隔开，避免跨段误拼），
    # 标记原文仍在正文 chunk 中，因此把整个 "[routing:XX]" 配成关键词也能命中。
    marker_words = []
    for chunk in chunks:
        for m in MARKER_RE.finditer(chunk or ""):
            word = m.group(1).strip()
            if word:
                marker_words.append(word)
    chunks.extend(marker_words)

    return "\n".join(chunk for chunk in chunks if chunk), unknown
