"""纯规则层：解析 4 组 provider 槽位与逐行 DSL，按关键词做确定性决策。不发起任何网络请求。

配置形态（v0.2 起，取代早期 rules_json/credentials_map 两份 JSON）：
  - 每个 provider 槽位（p1..p4）四个字段：
      pN_name       provider 显示名（仅用于报错与调试，不参与路由）
      pN_base_url   OpenAI 兼容 base_url（http/https）
      pN_key        API Key（secret 字段，可留空=无鉴权，例如本地服务）
      pN_models     模型与关键词，单行分号分隔 DSL：
                    "qwen-plus: 报价,合同 ; 别名=私有qwen3-coder: 代码,refactor ; glm-4"
                    段格式 `[路由名=]上游模型名[: 关键词1,关键词2]`；
                    `=` 左侧为路由 id（省略时即上游模型名），右侧为真实发给上游的模型名，
                    因此不同 provider 上的同名上游模型可以用不同路由名并存；
                    无冒号则该模型不配关键词（可作兜底）
  - default_target   未命中任何关键词时使用的模型名（必填）
  - uncheck_policy / uncheck_target  非文本内容策略（沿用 v0.1 语义）

规则语义（与 docs/dify-routing-plugin-plan.md 一致）：
  - 关键词按 casefold + NFC 归一后做子串匹配；中英文逗号、分号、冒号均可；
  - 目标顺序 = 优先级（槽位 p1→p4、槽内声明顺序），先命中先得；
  - 不做静默失败转移；配置错误必须显式报错；
  - 非文本内容按 uncheck_policy 处理：default_route / reject / fixed_target；
  - API Key 仅存在于各 provider 槽位的 secret 字段，绝不进入规则文本/日志/错误信息。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_MODELS_TEXT_BYTES = 64 * 1024
MAX_PROVIDERS = 4
MAX_TARGETS = 16
MAX_KEYWORDS_PER_TARGET = 32
MAX_NAME_CHARS = 40          # 路由名（别名）长度上限
MAX_UPSTREAM_CHARS = 120     # 上游真实模型名长度上限

UNCHECK_POLICIES = ("default_route", "reject", "fixed_target")

# 路由名（别名）不允许的字符：结构分隔符与空白（`=` 用作别名/上游名分隔，也不允许）
_MODEL_BAD_CHARS = set("；;:：,，= \t\r\n")


class ConfigError(ValueError):
    """路由配置不合法（DSL 结构、引用缺失、超限等）。"""


class UnknownContentRejected(Exception):
    """请求含无法按文本检查的内容段，且策略为 reject。"""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


@dataclass(frozen=True)
class Target:
    id: str            # 路由名：别名或上游模型名（全局唯一，default/uncheck_target 均引用它）
    provider: str      # provider 显示名（报错用）
    base_url: str
    api_key: str       # 可为空串（无鉴权上游）；不得出现在错误信息/日志中
    upstream: str      # 实际发给上游的模型名（允许跨 provider 重复）


@dataclass(frozen=True)
class Rule:
    id: str            # 等于目标路由名
    priority: int      # 全局声明顺序，越小越先
    model_id: str
    keywords: tuple    # 归一化后的关键词（str 元组）


@dataclass(frozen=True)
class Decision:
    target: Target
    rule_id: str | None
    reason: str  # rule:<id> | default | unknown_fixed_target


@dataclass(frozen=True)
class Policy:
    default_model: str
    targets: dict  # id -> Target
    rules: tuple   # 已按 (priority,) 排序，即声明顺序
    uncheck_policy: str
    uncheck_target: str

    def decide(self, text: str, unknown_parts: int) -> Decision:
        if unknown_parts > 0:
            if self.uncheck_policy == "reject":
                raise UnknownContentRejected(
                    f"请求包含 {unknown_parts} 个无法按文本检查的内容段，且未知内容策略为 reject"
                )
            if self.uncheck_policy == "fixed_target":
                return Decision(self.targets[self.uncheck_target], None, "unknown_fixed_target")
        hay = normalize(text)
        for rule in self.rules:
            for kw in rule.keywords:
                if kw in hay:
                    return Decision(self.targets[rule.model_id], rule.id, f"rule:{rule.id}")
        return Decision(self.targets[self.default_model], None, "default")


def _split_segments(models_text: str) -> list:
    """分号（中英文均可）切段；空段忽略。"""
    return [s.strip() for s in models_text.replace("；", ";").split(";") if s.strip()]


def _split_keywords(kw_part: str) -> list:
    """逗号（中英文均可）切关键词；strip 后丢弃空项。关键词内部允许空格。"""
    return [k.strip() for k in kw_part.replace("，", ",").split(",") if k.strip()]


def parse_providers(
    providers: list,  # [(slot_no, name, base_url, api_key, models_text)]
    default_target: str,
    uncheck_policy: str = "default_route",
    uncheck_target: str = "",
) -> Policy:
    targets: dict[str, Target] = {}
    rules: list[Rule] = []
    priority = 0

    if len(providers) > MAX_PROVIDERS:
        raise ConfigError(f"provider 槽位数量超过上限 {MAX_PROVIDERS}")

    for slot_no, name, base_url, api_key, models_text in providers:
        where = f"p{slot_no}"
        name = (name or "").strip()[:MAX_NAME_CHARS] or f"upstream{slot_no}"
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()
        models_text = (models_text or "").strip()

        configured = bool(base_url or models_text or api_key)
        if not configured:
            continue
        if not base_url:
            raise ConfigError(f"{where}（{name}）：填写了密钥或模型清单，但缺少 base_url")
        if not base_url.startswith(("http://", "https://")) or any(c.isspace() for c in base_url):
            raise ConfigError(f"{where}（{name}）：base_url 必须是 http(s) URL 且不含空白")
        segments = _split_segments(models_text)
        if not segments:
            raise ConfigError(f"{where}（{name}）：models 至少需要填写一个模型名")
        if len(models_text.encode("utf-8")) > MAX_MODELS_TEXT_BYTES:
            raise ConfigError(f"{where}：models 文本超过 {MAX_MODELS_TEXT_BYTES} 字节上限")

        for seg in segments:
            m = re.split(r"[：:]", seg, maxsplit=1)
            name_part = m[0].strip()
            kw_part = m[1] if len(m) > 1 else ""
            if not name_part:
                raise ConfigError(f"{where}（{name}）：存在空模型名（段 {seg!r}）")
            alias, sep, upstream_name = name_part.partition("=")
            alias = alias.strip()
            upstream_name = (upstream_name.strip() if sep else alias)
            if not alias:
                raise ConfigError(f"{where}（{name}）：存在空路由名（段 {seg!r}）")
            bad = _MODEL_BAD_CHARS.intersection(alias)
            if bad:
                raise ConfigError(f"{where}（{name}）：路由名 {alias!r} 含非法字符 {sorted(bad)}（`=` 用于“别名=上游模型名”）")
            if len(alias) > MAX_NAME_CHARS:
                raise ConfigError(f"{where}（{name}）：路由名 {alias!r} 超过 {MAX_NAME_CHARS} 字符")
            if not upstream_name:
                raise ConfigError(f"{where}（{name}）：{alias} 缺少上游模型名（`=` 右侧为空）")
            if any(c.isspace() for c in upstream_name) or len(upstream_name) > MAX_UPSTREAM_CHARS:
                raise ConfigError(f"{where}（{name}）：上游模型名 {upstream_name!r} 不合法（不允许空白，长度 ≤{MAX_UPSTREAM_CHARS}）")
            if alias in targets:
                raise ConfigError(f"路由名重复：{alias}（路由名需在所有 provider 间唯一；上游模型名可重复，请用别名区分）")
            targets[alias] = Target(alias, name, base_url, api_key, upstream_name)
            kws = _split_keywords(kw_part)
            if len(kws) > MAX_KEYWORDS_PER_TARGET:
                raise ConfigError(f"{where}（{name}）/{alias}：关键词数量超过上限 {MAX_KEYWORDS_PER_TARGET}")
            rules.append(Rule(alias, priority, alias, tuple(normalize(k) for k in kws)))
            priority += 1

    if len(targets) > MAX_TARGETS:
        raise ConfigError(f"模型总数超过上限 {MAX_TARGETS}")
    if not targets:
        raise ConfigError("至少需要配置一个 provider 槽位（含 base_url 与模型清单）")

    default_target = (default_target or "").strip()
    if not default_target:
        raise ConfigError("default_target（默认目标）必填：未命中关键词时使用的路由名（别名或模型名）")
    if default_target not in targets:
        raise ConfigError(f"default_target={default_target!r} 不是任何已配置的路由名（别名或模型名）")

    if uncheck_policy not in UNCHECK_POLICIES:
        raise ConfigError(f"uncheck_policy 必须是 {UNCHECK_POLICIES} 之一")
    if uncheck_policy == "fixed_target":
        uncheck_target = (uncheck_target or "").strip()
        if uncheck_target not in targets:
            raise ConfigError(f"uncheck_target={uncheck_target!r} 不是任何已配置的路由名（策略 fixed_target 时必填）")
    else:
        uncheck_target = (uncheck_target or "").strip()

    rules.sort(key=lambda item: item.priority)
    return Policy(default_target, targets, tuple(rules), uncheck_policy, uncheck_target)


def _seconds(name: str, raw, default: int, lo: int, hi: int) -> int:
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(str(raw).strip())))
    except ValueError:
        raise ConfigError(f"{name} 必须是整数秒") from None


def load_policy_from_credentials(credentials: dict):
    """从自定义模型实例凭据中解析出 (policy, connect_s, read_s)。

    provider 的连通性校验与 llm._invoke 共用此入口，保证校验与运行配置一致。
    API Key 只随 Target 返回，调用方不得写入错误信息或日志。
    """
    creds = credentials or {}
    providers = [
        (
            i,
            creds.get(f"p{i}_name") or "",
            creds.get(f"p{i}_base_url") or "",
            creds.get(f"p{i}_key") or "",
            creds.get(f"p{i}_models") or "",
        )
        for i in range(1, MAX_PROVIDERS + 1)
    ]
    policy = parse_providers(
        providers,
        creds.get("default_target") or "",
        (creds.get("uncheck_policy") or "default_route").strip(),
        creds.get("uncheck_target") or "",
    )
    connect_s = _seconds("connect_timeout", creds.get("connect_timeout"), 10, 1, 60)
    read_s = _seconds("read_timeout", creds.get("read_timeout"), 120, 5, 600)
    return policy, connect_s, read_s
