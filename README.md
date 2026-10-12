# Routed Keyword Model (routed-model)

Presents **one logical chat model** in Dify. On every request the plugin picks one
admin-configured **OpenAI-compatible upstream** using deterministic keyword rules, then
forwards the full conversation to that single target. End users select `routed-chat`
and never choose (or see) the real upstream model name.

> **Scope:** keyword routing is *business classification* (cheap vs. capable model,
> domain-specific prompts, etc.). It is **not** a security boundary or a
> data-loss-prevention control — keywords cannot prove what a request leaks.

## How it works

```
prompt_messages / tools / model_parameters
        │  extract_text (all text parts, tool-call args, tool defs, string params)
        ▼
   Policy.decide ── non-text content? ──► uncheck_policy: default_route | reject | fixed_target
        │ keyword match, casefold+NFC, priority asc, first hit wins
        ▼
   single target (base_url + model + API key from credentials_map)
        │  no silent failover — errors are surfaced, never retried on another target
        ▼
   OpenAI /chat/completions (stream or non-stream, tool calls passed through)
```

- Routing is re-decided **every turn**. Because Dify resends full history, once a
  conversation contains a keyword, later turns usually route to the same target
  ("sticky by history"). Tune rules with `priority` if you need different behavior.
- Unknown upstream model names never appear in results — responses always report the
  logical model name.

## Setup

1. Install this plugin, open the provider, and **add a custom model**, e.g. `routed-chat`.
2. Fill **Routing rules (JSON)** and **Credential map (JSON)** (see below).
3. Declare capabilities (`vision_support`, `function_calling_type: tool_call`, …) to
   match the weakest routed target; `context_size` / `max_tokens_to_sample` should be
   the *smallest* values among targets.
4. Saving validates the config and sends one minimal `ping` chat request to **every**
   target (consumes a few tokens per target).

### rules_json

```json
{
  "schema_version": 1,
  "default_model": "general",
  "models": [
    {
      "id": "general",
      "provider": "openai",
      "model": "gpt-4o-mini",
      "base_url": "https://api.openai.com/v1",
      "credential_ref": "openai-main"
    },
    {
      "id": "coder",
      "provider": "dashscope",
      "model": "qwen3-coder-plus",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "credential_ref": "dash-main"
    }
  ],
  "rules": [
    { "id": "code", "priority": 10, "model_id": "coder",
      "keywords": ["代码审查", "堆栈", "refactor", "stack trace"] }
  ]
}
```

Rules: keyword match is substring on `casefold()+NFC` text; rules run by `priority`
ascending (stable within equal priority); first hit wins; no hit → `default_model`.
Limits: ≤16 targets, ≤64 rules, ≤32 keywords/rule, document ≤64 KB
(if your Dify build caps the field lower, keep the JSON smaller).

### credentials_map (secret field)

```json
{ "openai-main": "sk-...", "dash-main": "sk-..." }
```

`credential_ref` in rules_json maps to a key here. Keys never appear in the rules
document, in responses, or in plugin logs.

### Unknown-content policy

Requests containing content that can't be keyword-checked (images on a vision-disabled
model, audio, files…) are counted. `uncheck_policy` decides:

| policy | behavior |
| --- | --- |
| `default_route` (default) | continue normal matching |
| `reject` | fail the request with an explicit error |
| `fixed_target` | always route to the target id in `uncheck_target` |

A target that genuinely cannot receive a content part (e.g. image with
`vision_support: no_support`) makes the request fail loudly rather than silently
drop the part.

## Field reference

| field | meaning |
| --- | --- |
| `mode` | only `chat` is supported |
| `rules_json` | targets + rules document (above) |
| `credentials_map` | secret JSON: `credential_ref -> api_key` |
| `uncheck_policy` / `uncheck_target` | unknown-content handling |
| `vision_support` | forward image parts as `image_url` (base64 data URLs or http urls) |
| `function_calling_type` | `tool_call` forwards Dify tool definitions as OpenAI `tools`; `no_call`/`function_call` drop them (agents: use `tool_call`) |
| `stream_function_calling` | declare upstream support for streamed tool-call deltas (accumulated and emitted as they arrive) |
| `agent_thought_support` / `structured_output_support` | capability flags for Dify UI/features; structured `response_format` is forwarded only when supported |
| `context_size` / `max_tokens_to_sample` | use the smallest values across routed targets |
| `connect_timeout` / `read_timeout` | per-request HTTP timeouts (seconds) |

## Errors

Configuration errors and upstream failures are raised as visible errors with the
target id and HTTP status; the plugin never tries another target after a failure.

## Privacy

See [PRIVACY.md](PRIVACY.md). Summary: request content is forwarded to the routed
target only; the plugin stores nothing, phones home nowhere, and logs no prompt or key.

## Development

```
routed-model/
├── manifest.yaml / main.py / requirements.txt   # dify-plugin==0.10.2, requests
├── policy.py            # pure rule engine (parse/validate/decide) — no I/O
├── message_extract.py   # text extraction + non-text counting
├── targets.py           # OpenAI-compatible payload/SSE client (single target)
├── provider/            # provider schema + save-time validation (pings every target)
└── models/llm/llm.py    # orchestration + result mapping
```

Package with the Dify plugin CLI:

```sh
dify plugin package ./routed-model -o routed_model-0.1.0.difypkg
```

Runtime requires the plugin daemon to install `requirements.txt` at add-time
(network egress to PyPI, or `PIP_MIRROR_URL` / offline repackaging in air-gapped setups).
