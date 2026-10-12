"""OpenAI 兼容上游层：payload 构造、HTTP/SSE 客户端。只与单个目标交互，不做失败转移。"""

from __future__ import annotations

import json

import requests

# 从 Dify model_parameters 透传的参数白名单（其余忽略，避免把未知字段塞给上游）
PARAM_PASSTHROUGH = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "logit_bias",
    "max_tokens",
    "max_completion_tokens",
)


class UpstreamError(Exception):
    """上游返回非成功状态、连接失败或响应不可解析。"""


def flatten_content(content, vision_support: bool) -> tuple:
    """把 Dify 消息内容转换为 OpenAI content；返回 (content, 无法转发段数)。"""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    texts = []
    parts = []
    dropped = 0
    for part in content or []:
        if isinstance(part, str):
            texts.append(part)
            continue
        ptype = getattr(part, "type", None)
        ptype = getattr(ptype, "value", ptype)
        if ptype == "text":
            text = getattr(part, "data", None)
            if not isinstance(text, str):
                text = getattr(part, "text", None)
            texts.append(text if isinstance(text, str) else "")
        elif ptype == "image" and vision_support:
            b64 = getattr(part, "base64_data", "") or ""
            url = getattr(part, "url", "") or ""
            if b64:
                fmt = getattr(part, "format", "") or "png"
                url = f"data:image/{fmt};base64,{b64}"
            if url:
                detail = getattr(getattr(part, "detail", None), "value", getattr(part, "detail", "low"))
                parts.append({"type": "image_url", "image_url": {"url": url, "detail": detail or "low"}})
            else:
                dropped += 1
        else:
            dropped += 1
    if not parts:
        return "".join(texts), dropped
    out = [{"type": "text", "text": t} for t in texts if t]
    out.extend(parts)
    return out, dropped


def build_messages(prompt_messages, vision_support: bool) -> tuple:
    """返回 (OpenAI messages 列表, 无法转发段数)。"""
    out = []
    dropped = 0
    for pm in prompt_messages:
        role = getattr(pm.role, "value", str(pm.role))
        content, d = flatten_content(getattr(pm, "content", None), vision_support)
        dropped += d
        if role == "assistant":
            tool_calls = []
            for tc in getattr(pm, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                tool_calls.append({
                    "id": getattr(tc, "id", "") or "",
                    "type": getattr(tc, "type", "") or "function",
                    "function": {
                        "name": getattr(fn, "name", "") or "",
                        "arguments": getattr(fn, "arguments", "") or "",
                    },
                })
            entry = {"role": "assistant"}
            entry["content"] = content if (content or not tool_calls) else None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        elif role == "tool":
            text_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            out.append({
                "role": "tool",
                "tool_call_id": getattr(pm, "tool_call_id", "") or "",
                "content": text_content,
            })
        else:
            # developer → system，兼容不接受 developer 角色的上游
            mapped = "system" if role == "developer" else role
            if mapped not in ("system", "user"):
                mapped = "user"
            out.append({"role": mapped, "content": content})
    return out, dropped


def build_payload(
    *,
    upstream_model: str,
    messages: list,
    model_parameters: dict | None,
    tools,
    stop,
    stream: bool,
    function_calling_type: str,
    structured_output_support: bool,
) -> dict:
    payload = {"model": upstream_model, "messages": messages}
    for key in PARAM_PASSTHROUGH:
        if model_parameters and model_parameters.get(key) is not None:
            payload[key] = model_parameters[key]
    if structured_output_support and model_parameters and model_parameters.get("response_format") is not None:
        payload["response_format"] = model_parameters["response_format"]
    if stop:
        payload["stop"] = list(stop)
    if function_calling_type == "tool_call" and tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": getattr(t, "name", "") or "",
                    "description": getattr(t, "description", "") or "",
                    "parameters": getattr(t, "parameters", None) or {},
                },
            }
            for t in tools
        ]
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def chat_completions(target, api_key: str, payload: dict, *, timeout, stream: bool) -> requests.Response:
    """向 target.base_url 发起一次 chat/completions 请求。非 200 / 连接异常统一抛 UpstreamError。"""
    url = target.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, stream=stream, timeout=timeout)
    except requests.RequestException as e:
        raise UpstreamError(f"连接上游 {target.id} 失败：{type(e).__name__}") from None
    if resp.status_code != 200:
        snippet = ""
        try:
            snippet = resp.text[:300]
        finally:
            resp.close()
        raise UpstreamError(f"上游 {target.id} 返回 HTTP {resp.status_code}：{snippet}")
    return resp


def iter_sse(resp: requests.Response):
    """解析 OpenAI 风格 SSE 流；[DONE] 结束，注释与空行跳过。"""
    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                raise UpstreamError(f"上游流式响应无法解析：{data[:120]}") from None
    finally:
        resp.close()
