"""纯规则层：解析并校验路由配置 JSON，按关键词做确定性决策。不发起任何网络请求。

配置契约（schema_version=1）：
  {"schema_version":1,
   "default_model":"<目标id>",
   "models":[{"id":...,"provider":...,"model":...,"base_url":...,"credential_ref":...}],
   "rules":[{"id":...,"priority":10,"model_id":...,"keywords":[...]}]}

规则语义（与 docs/dify-routing-plugin-plan.md 一致）：
  - 关键词按 casefold + NFC 归一后做子串匹配；
  - 按 priority 升序（越小越先），同优先级按声明顺序，命中第一条即路由；
  - 不做静默失败转移；配置错误必须显式报错；
  - 非文本内容按 uncheck_policy 处理：default_route / reject / fixed_target。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass

MAX_RULES_JSON_BYTES = 64 * 1024
MAX_TARGETS = 16
MAX_RULES = 64
MAX_KEYWORDS_PER_RULE = 32

UNCHECK_POLICIES = ("default_route", "reject", "fixed_target")


class ConfigError(ValueError):
    """路由配置不合法（JSON 结构、引用缺失、超限等）。"""


class UnknownContentRejected(Exception):
    """请求含无法按文本检查的内容段，且策略为 reject。"""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


@dataclass(frozen=True)
class Target:
    id: str
    provider: str
    model: str
    base_url: str
    credential_ref: str


@dataclass(frozen=True)
class Rule:
    id: str
    priority: int
    model_id: str
    keywords: tuple  # 归一化后的关键词（str 元组）


@dataclass(frozen=True)
class Decision:
    target: Target
    rule_id: str | None
    reason: str  # rule:<id> | default | unknown_fixed_target


@dataclass(frozen=True)
class Policy:
    default_model: str
    targets: dict  # id -> Target
    rules: tuple  # 已按 (priority, 声明顺序) 排序
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


def _require_str(obj: dict, key: str, where: str, allow_empty: bool = False) -> str:
    val = obj.get(key)
    if not isinstance(val, str):
        raise ConfigError(f"{where}.{key} 必须是字符串")
    if not allow_empty and not val.strip():
        raise ConfigError(f"{where}.{key} 不能为空")
    return val


def parse_policy(
    rules_json: str,
    credential_keys: set,
    uncheck_policy: str = "default_route",
    uncheck_target: str = "",
) -> Policy:
    if len(rules_json.encode("utf-8")) > MAX_RULES_JSON_BYTES:
        raise ConfigError(f"rules_json 超过 {MAX_RULES_JSON_BYTES} 字节上限")
    try:
        data = json.loads(rules_json)
    except ValueError as e:
        raise ConfigError(f"rules_json 不是合法 JSON：{e}") from None
    if not isinstance(data, dict):
        raise ConfigError("rules_json 顶层必须是对象")
    if data.get("schema_version") != 1:
        raise ConfigError("仅支持 schema_version=1")

    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError("models 必须是非空数组")
    if len(models) > MAX_TARGETS:
        raise ConfigError(f"目标数量超过上限 {MAX_TARGETS}")
    targets: dict[str, Target] = {}
    for i, m in enumerate(models):
        where = f"models[{i}]"
        if not isinstance(m, dict):
            raise ConfigError(f"{where} 必须是对象")
        tid = _require_str(m, "id", where)
        if tid in targets:
            raise ConfigError(f"{where}.id 重复：{tid}")
        provider = _require_str(m, "provider", where)
        model = _require_str(m, "model", where)
        base_url = _require_str(m, "base_url", where)
        if not base_url.startswith(("http://", "https://")) or any(c.isspace() for c in base_url):
            raise ConfigError(f"{where}.base_url 必须是 http(s) URL 且不含空白")
        cred_ref = _require_str(m, "credential_ref", where)
        if cred_ref not in credential_keys:
            raise ConfigError(f"{where}.credential_ref={cred_ref} 未在 credentials_map 中找到")
        targets[tid] = Target(tid, provider, model, base_url, cred_ref)

    default_model = _require_str(data, "default_model", "根")
    if default_model not in targets:
        raise ConfigError(f"default_model={default_model} 不在 models 中")

    rules_raw = data.get("rules")
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise ConfigError("rules 必须是数组")
    if len(rules_raw) > MAX_RULES:
        raise ConfigError(f"规则数量超过上限 {MAX_RULES}")
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for i, r in enumerate(rules_raw):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            raise ConfigError(f"{where} 必须是对象")
        rid = _require_str(r, "id", where)
        if rid in seen_ids:
            raise ConfigError(f"{where}.id 重复：{rid}")
        seen_ids.add(rid)
        priority = r.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ConfigError(f"{where}.priority 必须是整数")
        model_id = _require_str(r, "model_id", where)
        if model_id not in targets:
            raise ConfigError(f"{where}.model_id={model_id} 不在 models 中")
        kws = r.get("keywords")
        if not isinstance(kws, list) or not kws:
            raise ConfigError(f"{where}.keywords 必须是非空数组")
        if len(kws) > MAX_KEYWORDS_PER_RULE:
            raise ConfigError(f"{where}.keywords 数量超过上限 {MAX_KEYWORDS_PER_RULE}")
        norm_kws = []
        for kw in kws:
            if not isinstance(kw, str) or not kw.strip():
                raise ConfigError(f"{where}.keywords 必须是非空字符串数组")
            norm_kws.append(normalize(kw))
        rules.append(Rule(rid, priority, model_id, tuple(norm_kws)))
    rules.sort(key=lambda item: item.priority)  # 稳定排序，保留声明顺序

    if uncheck_policy not in UNCHECK_POLICIES:
        raise ConfigError(f"uncheck_policy 必须是 {UNCHECK_POLICIES} 之一")
    if uncheck_policy == "fixed_target" and uncheck_target not in targets:
        raise ConfigError(f"uncheck_target={uncheck_target} 不在 models 中（策略 fixed_target 必填）")

    return Policy(default_model, targets, tuple(rules), uncheck_policy, uncheck_target)


def load_policy_from_credentials(credentials: dict):
    """从自定义模型实例凭据中解析出 (policy, api_keys, connect_s, read_s)。

    provider 的连通性校验与 llm._invoke 共用此入口，保证校验与运行配置一致。
    """
    rules_json = (credentials.get("rules_json") or "").strip()
    raw_map = credentials.get("credentials_map") or ""
    try:
        api_keys = json.loads(raw_map)
    except ValueError as e:
        raise ConfigError(f"credentials_map 不是合法 JSON：{e}") from None
    if not isinstance(api_keys, dict) or not api_keys:
        raise ConfigError("credentials_map 必须是非空 JSON 对象")
    for ref, key in api_keys.items():
        if not isinstance(ref, str) or not ref.strip() or not isinstance(key, str) or not key.strip():
            raise ConfigError("credentials_map 的键与值必须都是非空字符串")

    def _seconds(name: str, default: int, lo: int, hi: int) -> int:
        val = credentials.get(name)
        if val in (None, ""):
            return default
        try:
            return max(lo, min(hi, int(str(val).strip())))
        except ValueError:
            raise ConfigError(f"{name} 必须是整数秒") from None

    policy = parse_policy(
        rules_json,
        set(api_keys.keys()),
        (credentials.get("uncheck_policy") or "default_route").strip(),
        (credentials.get("uncheck_target") or "").strip(),
    )
    return policy, dict(api_keys), _seconds("connect_timeout", 10, 1, 60), _seconds("read_timeout", 120, 5, 600)
