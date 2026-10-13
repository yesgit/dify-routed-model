# Routed Keyword Model (routed-model)

Source repository: <https://github.com/yesgit/dify-routed-model>

Presents **one logical chat model** in Dify. On every request the plugin picks one
admin-configured **OpenAI-compatible upstream model** using deterministic keyword rules,
then forwards the full conversation to that single target. End users select `routed-chat`
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
        │ keyword match, casefold+NFC, declaration order = priority, first hit wins
        ▼
   single target (provider base_url + API key + model)
        │  no silent failover — errors are surfaced, never retried on another target
        ▼
   OpenAI /chat/completions (stream or non-stream, tool calls passed through)
```

- Routing is re-decided **every turn**. Because Dify resends full history, once a
  conversation contains a keyword, later turns usually route to the same target
  ("sticky by history"). Reorder entries if you need different behavior.
- Upstream model names never appear in results — responses always report the
  logical model name.

## Setup

Configure everything in the custom model's credential form. There is **no JSON**:
up to **4 provider slots**, each with a name, base URL, API key, and a one-line
models/keywords list.

### Provider slots (`p1..p4`)

| field | meaning |
| --- | --- |
| `pN_name` | display name, used in error messages only |
| `pN_base_url` | OpenAI-compatible base URL, e.g. `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `pN_key` | API key — stored in Dify's **encrypted secret field for this provider**; leave empty for auth-free local gateways |
| `pN_models` | models of this provider plus their routing keywords (syntax below) |

A slot is active as soon as any of its fields is filled; `pN_models` and
`pN_base_url` are then both required.

### Models/keywords syntax (one line, semicolon-separated)

```
qwen-plus: 报价,合同,legal ; coder=qwen3-coder-plus: 代码审查,堆栈,refactor ; glm-4
```

- Each entry is `[route-name=]model: keyword1,keyword2` — the part after `:` may
  contain spaces (e.g. `stack trace`). Entries without `:` are valid (target with no
  keywords, usable only as default/uncheck target).
- **`route-name=` alias**: the id used everywhere (keywords, `default_target`,
  `uncheck_target`) is the route name, while the right side of `=` is the model name
  actually sent upstream. Omit `=` and the model name doubles as the route name.
  This lets **two providers expose the same upstream model** (e.g. DashScope
  `qwen-plus` and a private gateway's `qwen-plus`) as separate routing targets:
  `ds=qwen-plus: legal ; local=qwen-plus`. Route names must be globally unique;
  upstream model names may repeat.
- Entries are evaluated **in declaration order across all slots (p1 → p4)**; the first
  target with any keyword found (substring, casefold + NFC) wins. Chinese full-width
  punctuation (`；` `：` `，`) also works.
- Limits: ≤4 providers, ≤16 models total, ≤32 keywords per model, ≤64 KB per line.
- The input is single-line in Dify's UI; if you paste text with newlines, they are
  stripped — the grammar above is newline-free by design.

### `default_target` (required)

**Route name** (alias, or the model name when no alias) used when no keyword hits
anywhere, e.g. `qwen-plus` or `ds`.

### Unknown-content policy

Requests containing content that can't be keyword-checked (images on a vision-disabled
model, audio, files…) are counted. `uncheck_policy` decides:

| policy | behavior |
| --- | --- |
| `default_route` (default) | continue normal matching |
| `reject` | fail the request with an explicit error |
| `fixed_target` | always route to the route-name set in `uncheck_target` |

A target that genuinely cannot receive a content part (e.g. image with
`vision_support: no_support`) makes the request fail loudly rather than silently
drop the part.

### Capability flags

Declare `vision_support` / `function_calling_type` / `structured_output_support` etc.
to match the **weakest** routed target; set `context_size` / `max_tokens_to_sample` to
the **smallest** values among targets. `connect_timeout` / `read_timeout` bound the
per-request HTTP call.

### Full example

```
p1_name      = tongyi
p1_base_url  = https://dashscope.aliyuncs.com/compatible-mode/v1
p1_key       = sk-...                      (secret)
p1_models    = qwen-plus: 报价,合同; coder=qwen3-coder-plus: 代码审查,refactor,堆栈

p2_name      = private-gateway
p2_base_url  = https://llm.internal.example/v1
p2_key       =                              (empty = no auth)
p2_models    = local=qwen-plus              (same upstream model as p1, different route)

default_target = qwen-plus                  (route name; here p1's model without alias)
```

`A 1.8.1 note:` saving the model sends one minimal `ping` chat request to **every**
configured model (a few tokens per model) as a connectivity check.

## Errors

Configuration errors and upstream failures are raised as visible errors with the
model name and HTTP status; the plugin never tries another target after a failure.
Error messages never contain API keys.

## Privacy

See [PRIVACY.md](PRIVACY.md). Summary: request content is forwarded to the routed
target only; API keys live in Dify's encrypted credential storage; the plugin stores
nothing, phones home nowhere, and logs no prompt or key.

## Development

```
routed-model/
├── manifest.yaml / main.py / requirements.txt   # dify-plugin==0.10.2, requests
├── policy.py            # pure rule engine (DSL parse/validate/decide) — no I/O
├── message_extract.py   # text extraction + non-text counting
├── targets.py           # OpenAI-compatible payload/SSE client (single target)
├── provider/            # provider schema + save-time validation (pings every target)
└── models/llm/llm.py    # orchestration + result mapping
```

Package with the Dify plugin CLI:

```sh
dify plugin package ./routed-model -o routed_model-0.2.3.difypkg
```

Runtime requires the plugin daemon to install `requirements.txt` at add-time
(network egress to PyPI, or `PIP_MIRROR_URL` / offline repackaging in air-gapped setups).
