"""保存时的凭据校验：解析配置 + 按"校验强度"决定是否发送连通性 ping。

provider 会话（validate_provider_credentials）与自定义模型会话
（LargeLanguageModel.validate_credentials）共用此入口，保证两个入口的
校验行为一致。本模块可能发起网络请求；配置问题抛 ConfigError，
连通性问题抛 ConnectivityError（消息含目标名，不含密钥）。

connectivity_check（缺省 ping_all，行为与旧版一致）：
  - ping_all     解析 + 依次 ping 全部目标，任一失败即保存失败
  - ping_default 解析 + 仅 ping 兜底目标（其余目标首次被路由到时才暴露问题）
  - parse_only   仅本地解析，不发任何网络请求
"""

from __future__ import annotations

from policy import ConfigError, load_policy_from_credentials
from targets import chat_completions

CHECK_LEVELS = ("ping_all", "ping_default", "parse_only")


class ConnectivityError(Exception):
    """某个目标不可达/报错。消息可安全展示给用户（不含密钥）。"""


def _ping_target(target, connect_s: int, read_s: int) -> None:
    payload = {
        "model": target.upstream,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    try:
        resp = chat_completions(
            target, payload,
            timeout=(connect_s, min(read_s, 30)), stream=False,
        )
    except Exception as e:  # 包括 UpstreamError 与 requests 异常
        raise ConnectivityError(f"目标 {target.id} 连通性校验失败：{e}") from None
    try:
        resp.json()
    except ValueError:
        pass  # 200 但非 JSON：视为可达
    finally:
        resp.close()


def validate_and_ping(credentials: dict) -> None:
    """解析 4 槽位配置，并按 connectivity_check 决定 ping 范围。

    :raises ConfigError: 路由配置不合法（或 connectivity_check 取值不合法）
    :raises ConnectivityError: 某个被检查的目标 ping 失败（不做失败转移，立即抛出）
    """
    creds = credentials or {}
    level = (creds.get("connectivity_check") or "ping_all").strip() or "ping_all"
    if level not in CHECK_LEVELS:
        raise ConfigError(f"connectivity_check 必须是 {CHECK_LEVELS} 之一，当前为 {level!r}")

    policy, connect_s, read_s = load_policy_from_credentials(creds)

    if level == "parse_only":
        return
    if level == "ping_default":
        # 兜底目标必然存在（load_policy 已校验其引用）
        _ping_target(policy.targets[policy.default_model], connect_s, read_s)
        return
    # ping_all：按声明顺序逐个 ping，任一失败立即抛出
    for target in policy.targets.values():
        _ping_target(target, connect_s, read_s)
