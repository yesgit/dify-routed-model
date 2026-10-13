"""Routed-model LLM 入口：提取文本 → 规则决策 → 单目标 OpenAI 兼容调用 → 映射回 Dify 结果。

原则（方案 方式A）：
  - 决策为纯函数（policy 层），本文件只做编排与映射；
  - 一次请求只打一个目标，失败即报错，绝不静默换目标；
  - 无法转发给上游的非文本段直接报错，不悄悄丢弃；
  - 日志/错误信息不包含密钥与原始请求内容。
"""

from __future__ import annotations

import time
from collections.abc import Generator
from decimal import Decimal
from typing import NamedTuple

from dify_plugin.entities.model.llm import (
    LLMResult,
    LLMResultChunk,
    LLMResultChunkDelta,
    LLMUsage,
)
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessage,
    PromptMessageTool,
)
from dify_plugin.errors.model import CredentialsValidateFailedError, InvokeError
from dify_plugin.interfaces.model.large_language_model import LargeLanguageModel

from message_extract import extract_text
from policy import ConfigError, Policy, UnknownContentRejected, load_policy_from_credentials
from targets import UpstreamError, build_messages, build_payload, chat_completions, iter_sse
from validation import ConnectivityError, validate_and_ping

_BUNDLE_CACHE_LIMIT = 8


_CACHE_KEYS = [
    "default_target", "uncheck_policy", "uncheck_target",
    "vision_support", "function_calling_type", "structured_output_support",
    "connect_timeout", "read_timeout",
]
for _i in range(1, 5):
    for _f in ("name", "base_url", "key", "models"):
        _CACHE_KEYS.append(f"p{_i}_{_f}")

_CREDENTIAL_FIELDS = tuple(sorted(_CACHE_KEYS))


# 注意：本模块由 SDK 通过 spec_from_file_location 加载，exec 时 `models.llm.llm` 可能尚未
# 注册进 sys.modules；Python 3.12 的 @dataclass 会在字段解析时查询该模块命名空间而直接崩溃
# （插件启动即挂）。故此处使用 NamedTuple 而非 dataclass，policy.py 等常规导入的模块不受影响。
class Bundle(NamedTuple):
    policy: Policy
    vision_support: bool
    function_calling_type: str
    structured_output_support: bool
    connect_s: int
    read_s: int


def _flag(credentials: dict, name: str, value: str) -> bool:
    return (credentials.get(name) or "").strip() == value


def _estimate_tokens(text: str) -> int:
    """无上游 usage 时的粗估：CJK 等非 ASCII 按 1 token/字，ASCII 按 4 字/token。"""
    non_ascii = sum(1 for c in text if ord(c) > 127)
    ascii_count = len(text) - non_ascii
    return max(1, non_ascii + ascii_count // 4)


def _make_usage(usage: dict | None, prompt_est: int, completion_est: int, latency: float) -> LLMUsage:
    usage = usage or {}
    pt = int(usage.get("prompt_tokens", prompt_est) or prompt_est)
    ct = int(usage.get("completion_tokens", completion_est) or completion_est)
    zero = Decimal("0")
    return LLMUsage(
        prompt_tokens=pt,
        prompt_unit_price=zero,
        prompt_price_unit=zero,
        prompt_price=zero,
        completion_tokens=ct,
        completion_unit_price=zero,
        completion_price_unit=zero,
        completion_price=zero,
        total_tokens=int(usage.get("total_tokens") or (pt + ct)),
        total_price=zero,
        currency="USD",
        latency=latency,
    )


def _map_tool_calls(raw_list: list) -> list:
    calls = []
    for i, tc in enumerate(raw_list or []):
        fn = tc.get("function") or {}
        calls.append(
            AssistantPromptMessage.ToolCall(
                id=tc.get("id") or f"call_{i}",
                type=tc.get("type") or "function",
                function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                    name=fn.get("name") or "",
                    arguments=fn.get("arguments") or "",
                ),
            )
        )
    return calls


class RoutedModelLargeLanguageModel(LargeLanguageModel):
    # SDK（dify-plugin 0.10.2）的 AIModel 要求实现这两个抽象成员：保存自定义模型实例时
    # core 触发的是 validate_credentials（而非 provider 的
    # validate_provider_credentials），缺失会导致类无法实例化（HTTP 500）。

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """保存/添加自定义模型实例时调用：解析配置并向每个已配置目标 ping 一次。

        `model` 为逻辑模型名（实例名），不参与路由，也不写入任何请求。
        """
        try:
            validate_and_ping(credentials or {})
        except ConfigError as e:
            raise CredentialsValidateFailedError(f"路由配置无效：{e}") from None
        except ConnectivityError as e:
            raise CredentialsValidateFailedError(str(e)) from None

    @property
    def _invoke_error_mapping(self) -> dict[type[InvokeError], list[type[Exception]]]:
        # 空映射 = SDK 兜底把异常包装为 "[routed_model] Error: <原文>"，保留插件自己的
        # 中文报错与"不做失败转移"提示；若在此登记 InvokeError，消息会被套上
        # "None, " 前缀（基类无 description），故不登记任何类型。
        return {}

    # 简单 LRU 式缓存：凭据字符串不变时复用解析结果，避免每 token 请求重解析 DSL
    _bundles: dict = {}

    def _bundle(self, credentials: dict) -> Bundle:
        creds = credentials or {}
        cache_key = tuple(str(creds.get(k, "")) for k in _CREDENTIAL_FIELDS)
        bundle = self._bundles.get(cache_key)
        if bundle is not None:
            return bundle
        policy, connect_s, read_s = load_policy_from_credentials(creds)
        bundle = Bundle(
            policy=policy,
            vision_support=_flag(creds, "vision_support", "support"),
            function_calling_type=(creds.get("function_calling_type") or "no_call").strip(),
            structured_output_support=_flag(creds, "structured_output_support", "supported"),
            connect_s=connect_s,
            read_s=read_s,
        )
        if len(self._bundles) >= _BUNDLE_CACHE_LIMIT:
            self._bundles.pop(next(iter(self._bundles)))
        self._bundles[cache_key] = bundle
        return bundle

    def _invoke(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        model_parameters: dict,
        tools: list[PromptMessageTool] | None = None,
        stop: list[str] | None = None,
        stream: bool = True,
        user: str | None = None,
    ) -> LLMResult | Generator[LLMResultChunk, None, None]:
        creds = credentials or {}
        if (creds.get("mode") or "chat") != "chat":
            raise ValueError("routed-model 仅支持 chat 模式")
        try:
            bundle = self._bundle(creds)
        except ConfigError as e:
            raise ValueError(f"路由配置无效：{e}") from None

        text, unknown_parts = extract_text(prompt_messages, tools, model_parameters)
        try:
            decision = bundle.policy.decide(text, unknown_parts)
        except UnknownContentRejected as e:
            raise ValueError(str(e)) from None
        target = decision.target

        messages, dropped = build_messages(prompt_messages, vision_support=bundle.vision_support)
        if dropped:
            raise ValueError(
                f"请求包含 {dropped} 个当前配置无法转发的非文本内容段"
                "（如未开启视觉支持的图片、音频、文件等），已拒绝以避免静默丢失"
            )

        payload = build_payload(
            upstream_model=target.upstream,
            messages=messages,
            model_parameters=model_parameters,
            tools=tools,
            stop=stop,
            stream=stream,
            function_calling_type=bundle.function_calling_type,
            structured_output_support=bundle.structured_output_support,
        )
        try:
            resp = chat_completions(
                target, payload,
                timeout=(bundle.connect_s, bundle.read_s), stream=stream,
            )
        except UpstreamError as e:
            raise ValueError(f"路由到目标 {target.id} 失败（不做失败转移）：{e}") from None

        start = time.time()
        if stream:
            return self._stream(resp, model, target.id, prompt_messages, _estimate_tokens(text), start)
        return self._complete(resp, model, prompt_messages, _estimate_tokens(text), start)

    def _complete(self, resp, model, prompt_messages, prompt_est, start) -> LLMResult:
        try:
            data = resp.json()
        except ValueError:
            raise ValueError("上游返回不是合法 JSON") from None
        finally:
            resp.close()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("上游响应缺少 choices")
        message = choices[0].get("message") or {}
        return LLMResult(
            model=model,
            prompt_messages=prompt_messages,
            message=AssistantPromptMessage(
                content=message.get("content") or "",
                tool_calls=_map_tool_calls(message.get("tool_calls") or []),
            ),
            usage=_make_usage(data.get("usage"), prompt_est, _estimate_tokens(message.get("content") or ""), time.time() - start),
        )

    def _stream(self, resp, model, target_id, prompt_messages, prompt_est, start) -> Generator[LLMResultChunk, None, None]:
        pending: dict[int, dict] = {}
        usage: dict | None = None
        completion_chars = ""
        index = 0
        finished = False
        try:
            for event in self._wrap_stream_events(iter_sse(resp), target_id):
                if event.get("usage"):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                finish_reason = choice.get("finish_reason")
                content = delta.get("content") or ""

                tool_calls = None
                for td in delta.get("tool_calls") or []:
                    slot = pending.setdefault(int(td.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                    if td.get("id"):
                        slot["id"] = td["id"]
                    fn = td.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if pending:
                    tool_calls = _map_tool_calls([
                        {"id": s["id"], "type": "function",
                         "function": {"name": s["name"], "arguments": s["arguments"]}}
                        for _, s in sorted(pending.items())
                    ])

                if not content and not tool_calls and not finish_reason:
                    continue
                completion_chars += content

                kwargs = {}
                if finish_reason:
                    finished = True
                    kwargs["usage"] = _make_usage(usage, prompt_est, _estimate_tokens(completion_chars), time.time() - start)
                    kwargs["finish_reason"] = finish_reason
                yield LLMResultChunk(
                    model=model,  # 对外始终是逻辑模型名，不泄露上游真实模型名
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=index,
                        message=AssistantPromptMessage(content=content or "", tool_calls=tool_calls or []),
                        **kwargs,
                    ),
                )
                index += 1
            if not finished:
                # 上游未发 finish_reason 就结束：补一个收尾块，保证 usage 与结束状态可判定
                yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=index,
                        message=AssistantPromptMessage(content="", tool_calls=[]),
                        usage=_make_usage(usage, prompt_est, _estimate_tokens(completion_chars), time.time() - start),
                        finish_reason="stop",
                    ),
                )
        finally:
            resp.close()

    @staticmethod
    def _wrap_stream_events(events, target_id: str):
        """把流中的 UpstreamError 转成带目标标识的可读错误；不吞错、不重试。"""
        try:
            yield from events
        except UpstreamError as e:
            raise ValueError(f"流式路由到目标 {target_id} 中断（不做失败转移）：{e}") from None

    def get_num_tokens(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        tools: list[PromptMessageTool] | None = None,
    ) -> int:
        # 无上游 tokenizer 可依：按混合文本粗估（非 ASCII 1/字，ASCII 4 字/token）
        text, _ = extract_text(prompt_messages, tools, None)
        return _estimate_tokens(text)
