# 关键词路由模型（routed-model）

在 Dify 中对外呈现**一个逻辑聊天模型**：每次请求由插件按确定性关键词规则，从管理员配置的
**OpenAI 兼容上游**中选择**一个**目标并转发完整对话。最终用户只选择 `routed-chat`，
不能（也无法）看到或指定真实上游模型名。

> **定位：** 关键词路由是**业务分流**（便宜模型/强模型、领域请求等），**不是**安全边界，
> 也不构成防泄漏控制——关键词无法证明请求会泄露什么。

## 工作机制

```
prompt_messages / tools / model_parameters
        │  提取全部可检查文本（消息文本、工具调用参数、工具定义、字符串参数）
        ▼
   规则决策 ── 含无法文本检查的内容？──► uncheck_policy: default_route | reject | fixed_target
        │ 关键词 casefold+NFC 子串匹配；priority 升序，命中第一条即路由
        ▼
   单一目标（base_url + 上游模型名 + credentials_map 中的密钥）
        │ 无静默失败转移：出错直接报错，绝不换目标重试
        ▼
   OpenAI /chat/completions（流式/非流式，工具调用透传）
```

- 路由**每轮重新决策**。Dify 每轮都会重发完整历史，因此对话里一旦出现关键词，后续轮次
  通常会继续路由到同一目标（"历史黏性"）。需要不同行为时用 `priority` 调整规则。
- 响应中的模型名始终是逻辑模型名，上游真实模型名不外泄。

## 配置步骤

1. 安装插件，进入供应商，**添加自定义模型**（如 `routed-chat`）。
2. 填写「路由规则 JSON」与「密钥映射 JSON」（见下）。
3. 能力声明（`vision_support`、`function_calling_type: tool_call` 等）按各路由目标中
   **最弱**的配置填写；`context_size` / `max_tokens_to_sample` 填**最小值**。
4. 保存时会校验配置，并向**每个**目标发送一次最小 `ping` 请求（消耗少量 token）。

### rules_json 格式

```json
{
  "schema_version": 1,
  "default_model": "general",
  "models": [
    { "id": "general", "provider": "openai", "model": "gpt-4o-mini",
      "base_url": "https://api.openai.com/v1", "credential_ref": "openai-main" },
    { "id": "coder", "provider": "dashscope", "model": "qwen3-coder-plus",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "credential_ref": "dash-main" }
  ],
  "rules": [
    { "id": "code", "priority": 10, "model_id": "coder",
      "keywords": ["代码审查", "堆栈", "refactor", "stack trace"] }
  ]
}
```

规则语义：关键词按 `casefold()+NFC` 归一后做子串匹配；按 `priority` 从小到大执行
（同级保持声明顺序）；命中第一条即路由；全不命中走 `default_model`。
上限：目标 ≤16、规则 ≤64、每条规则关键词 ≤32、文档 ≤64KB。

### credentials_map（密文字段）

```json
{ "openai-main": "sk-...", "dash-main": "sk-..." }
```

规则中的 `credential_ref` 对应这里的键。密钥不出现在规则文档、模型输出和日志中。

### 未知内容策略（uncheck_policy）

请求中无法按文本检查的内容段（未开视觉时的图片、音频、文件等）会被计数：

| 取值 | 行为 |
| --- | --- |
| `default_route`（默认） | 照常走规则与默认目标 |
| `reject` | 显式报错拒绝请求 |
| `fixed_target` | 固定走 `uncheck_target` 指定的目标 |

目标实际无法接收的内容段（如 `vision_support: no_support` 时出现图片）会直接报错，
而不是悄悄丢弃内容。

## 字段说明

| 字段 | 含义 |
| --- | --- |
| `mode` | 仅支持 `chat` |
| `rules_json` | 目标清单与规则文档 |
| `credentials_map` | 密文 JSON：`credential_ref -> api_key` |
| `uncheck_policy` / `uncheck_target` | 未知内容处理策略 |
| `vision_support` | 开启后图片段转 `image_url`（base64 data URL 或 http url） |
| `function_calling_type` | `tool_call` 将 Dify 工具定义按 OpenAI `tools` 透传（Agent 场景推荐）；其余丢弃工具定义 |
| `stream_function_calling` | 声明上游支持流式工具调用（增量参数会被累积后输出） |
| `agent_thought_support` / `structured_output_support` | 能力声明；结构化输出仅在支持时透传 `response_format` |
| `context_size` / `max_tokens_to_sample` | 填各目标中的最小值 |
| `connect_timeout` / `read_timeout` | 每次请求的 HTTP 超时（秒） |

## 错误行为

配置错误与上游失败都会带目标 id 与 HTTP 状态显式抛出；插件从不自动换目标重试。

## 隐私

见 PRIVACY.md。要点：请求内容只发给被路由的那一个目标；插件不存储、不遥测、
日志不含密钥与原文。安装时需插件守护进程可达 PyPI（或使用镜像/离线打包）。
