# 关键词路由模型（routed-model）

在 Dify 中对外呈现**一个逻辑聊天模型**：每次请求由插件按确定性关键词规则，从管理员配置的
**OpenAI 兼容上游模型**中选择**一个**目标并转发完整对话。最终用户只选择 `routed-chat`，
不能（也无法）看到或指定真实上游模型名。

> **定位：** 关键词路由是**业务分流**（便宜模型/强模型、领域请求等），**不是**安全边界，
> 也不构成防泄漏控制——关键词无法证明请求会泄露什么。

## 工作机制

```
prompt_messages / tools / model_parameters
        │  提取全部可检查文本（消息文本、工具调用参数、工具定义、字符串参数）
        ▼
   规则决策 ── 含无法文本检查的内容？──► uncheck_policy: default_route | reject | fixed_target
        │ 关键词 casefold+NFC 子串匹配；声明顺序即优先级，先命中先得
        ▼
   单一目标（该 provider 的 base_url + API Key + 模型名）
        │ 无静默失败转移：出错直接报错，绝不换目标重试
        ▼
   OpenAI /chat/completions（流式/非流式，工具调用透传）
```

- 路由**每轮重新决策**。Dify 每轮都会重发完整历史，因此对话里一旦出现关键词，后续轮次
  通常会继续路由到同一目标（"历史黏性"）。需要不同行为时调整条目顺序。
- 响应中的模型名始终是逻辑模型名，上游真实模型名不外泄。

## 配置步骤（全程无 JSON）

1. 安装插件，进入供应商，**添加自定义模型**（如 `routed-chat`）。
2. 最多填 **4 个 provider 槽位**（p1–p4），每个槽位四个字段；再填**默认目标**。
3. 能力声明（`vision_support`、`function_calling_type: tool_call` 等）按各路由目标中
   **最弱**的配置填写；`context_size` / `max_tokens_to_sample` 填**最小值**。
4. 保存时会校验配置；默认向**每个**模型发送一次最小 `ping` 请求（消耗少量 token），
   可用 `connectivity_check` 放宽为只校验兜底目标或仅做本地配置检查（见字段说明）。

### provider 槽位

| 字段 | 含义 |
| --- | --- |
| `pN_name` | 显示名，仅用于报错信息 |
| `pN_base_url` | OpenAI 兼容 base_url，如 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `pN_key` | API Key，存放于 Dify **该 provider 的密文凭据字段**；本地无鉴权网关可留空 |
| `pN_models` | 该 provider 下的模型与路由关键词，可用“路由名=模型名”别名（单行语法见下） |

只要槽位任一字段非空即视为启用，启用时 `pN_base_url` 与 `pN_models` 均必填。

### 模型/关键词行语法（单行、分号分隔）

```
qwen-plus: 报价,合同,legal ; coder=qwen3-coder-plus: 代码审查,堆栈,refactor ; glm-4
```

- 每个条目形如 `[路由名=]模型名: 关键词1,关键词2`；冒号后的关键词允许含空格（如
  `stack trace`）。不带冒号的条目也合法（该模型只作为兜底/固定目标）。
- **`路由名=` 别名**：规则、`default_target`、`uncheck_target` 引用的都是**路由名**；
  `=` 右侧才是真正发给上游的模型名。省略 `=` 时，模型名本身即路由名。这样
  **两个 provider 提供同名模型**（如官方与私有网关都有 `qwen-plus`）也能各自成为
  路由目标：`ds=qwen-plus: 报价 ; local=qwen-plus`。路由名必须全局唯一，
  上游模型名允许重复。
- 所有槽位按 **p1 → p4、槽内声明顺序** 依次比较，**先命中先得**；中英文标点
  （`；` `：` `，`）均可。
- 上限：provider ≤4、模型总数 ≤16、每模型关键词 ≤32、单行 ≤64KB。
- Dify 表单为单行输入框：粘贴带换行的文本会被去掉换行，请保持一行。

### default_target（默认目标，必填）

任何关键词都未命中时使用的**路由名**（别名；没起别名时就是模型名），例如 `qwen-plus`
或 `ds`。

### 节点级路由提示（route_keywords）

LLM 节点的模型参数区多出一个 `route_keywords` 字符串参数（由**应用开发者在节点里配置**，
最终用户无法触碰）：

- 参数值会被并入本节点的路由匹配文本，可以把某个节点固定路由到指定目标，
  且**不污染发给模型的提示词**；
- **绝不转发上游**（payload 白名单不含它）；
- 只有 `pN_models` 行里已定义的关键词才能命中它，规则优先级不变（声明顺序、先命中先得），
  写了没定义的词等于没写；
- Dify 的模型参数是节点里的静态值，**不能引用工作流变量**。需要"每次请求动态决定"时，
  让上游分类器把路由词写进提示词即可——提示词文本本来就被同样匹配。

### 未知内容策略（uncheck_policy）

请求中无法按文本检查的内容段（未开视觉时的图片、音频、文件等）会被计数：

| 取值 | 行为 |
| --- | --- |
| `default_route`（默认） | 忽略这些段，其余文本照常匹配关键词：命中走该规则，未命中走兜底 |
| `reject` | 显式报错拒绝请求 |
| `fixed_target` | 只要含任一不可检段就无条件走 `uncheck_target` 指定的路由名（在关键词匹配之前生效） |

该策略**只在请求真的含有不可检内容段时才生效**：纯文本请求下三种设置行为完全一致；
`fixed_target` 优先于关键词匹配，适合把带附件（图片/音频/文件）的请求统一收口到一个
能处理的目标。开启 `debug_log` 后可用日志里的 `reason=` 区分走的路径
（`rule:<id>` / `default` / `unknown_fixed_target`）。

目标实际无法接收的内容段（如 `vision_support: no_support` 时出现图片）会直接报错，
而不是悄悄丢弃内容。

### 生产平台 INSTALL_METHOD 兼容（aws_lambda 等）

部分生产部署（K8s / 插件网关，以 Lambda 风格调起插件）会向插件进程注入
`INSTALL_METHOD=aws_lambda`；官方 dify-plugin SDK（含 0.10.2 在内的所有发布版本）
只认 `local|remote|serverless`，插件会在 **import 阶段** 崩溃——保存凭据报的是
启动错误，与配置内容无关。插件在导入 SDK 前对该变量做规范化：未知取值映射为
`serverless`（SDK 在插件进程内起 HTTP 服务，监听 `SERVERLESS_HOST:SERVERLESS_PORT`，
默认 `0.0.0.0:8080`，符合 Lambda 风格调用语义）。若平台实际走 stdio 管道，在部署侧
设置 `ROUTED_MODEL_INSTALL_METHOD=local` 即可覆盖映射，无需重新打包。
官方 docker-compose 部署（`local` 或未设置该变量）不受任何影响。

## 完整示例

```
p1_name      = tongyi
p1_base_url  = https://dashscope.aliyuncs.com/compatible-mode/v1
p1_key       = sk-...            （密文字段）
p1_models    = qwen-plus: 报价,合同; coder=qwen3-coder-plus: 代码审查,refactor,堆栈

p2_name      = 私有网关
p2_base_url  = https://llm.internal.example/v1
p2_key       =                    （留空 = 无鉴权）
p2_models    = local=qwen-plus    （与 p1 同名上游，不同路由名）

default_target = qwen-plus        （填路由名：别名，或未起别名时的模型名）
```

## 字段说明

| 字段 | 含义 |
| --- | --- |
| `mode` | 仅支持 `chat` |
| `p1..p4` | 最多 4 个上游 provider（名称/base_url/密钥/模型清单） |
| `default_target` | 未命中关键词时的路由名（别名或模型名） |
| `route_keywords`（节点参数） | 节点级路由提示：并入匹配文本、不发给上游；只能命中已配置关键词；为节点静态配置，不能引用变量 |
| `uncheck_policy` / `uncheck_target` | 未知内容处理策略（fixed_target 用的也是路由名） |
| `connectivity_check` | 保存时校验强度：`ping_all`（默认，逐个 ping）/ `ping_default`（只 ping 兜底目标）/ `parse_only`（不联网，只本地解析）。不影响运行时路由；某目标暂时不可达时可用后两档先把配置存进去 |
| `debug_log` | 决策调试日志：选"记录决策"后每次请求向插件 daemon 日志（docker logs）写一行元数据（逻辑模型/路由名/命中原因/流式/条数字符数），绝不含 prompt、密钥、URL；排障后建议关回"关闭" |
| `vision_support` | 开启后图片段转 `image_url`（base64 data URL 或 http url） |
| `function_calling_type` | `tool_call` 将 Dify 工具定义按 OpenAI `tools` 透传（Agent 场景推荐）；其余丢弃工具定义 |
| `stream_function_calling` | 声明上游支持流式工具调用（增量参数会被累积后输出） |
| `agent_thought_support` / `structured_output_support` | 能力声明；结构化输出仅在支持时透传 `response_format` |
| `context_size` / `max_tokens_to_sample` | 填各目标中的最小值 |
| `connect_timeout` / `read_timeout` | 每次请求的 HTTP 超时（秒） |

## 错误行为

配置错误与上游失败都会带模型名与 HTTP 状态显式抛出；报错信息**不含任何密钥**；
插件从不自动换目标重试。

## 隐私

见 PRIVACY.md。要点：请求内容只发给被路由的那一个目标；密钥存于 Dify 密文凭据字段；
插件不存储、不遥测、日志不含密钥与原文。安装时需插件守护进程可达 PyPI（或使用镜像/离线打包）。
