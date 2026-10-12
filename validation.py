"""保存时的连通性校验：解析配置并向每个目标发送一次最小 ping。

provider 会话（validate_provider_credentials）与自定义模型会话
（LargeLanguageModel.validate_credentials）共用此入口，保证两个入口的
校验行为一致。本模块会发起网络请求；配置问题抛 ConfigError，
连通性问题抛 ConnectivityError（消息含目标名，不含密钥）。
"""

from __future__ import annotations

from policy import load_policy_from_credentials
from targets import chat_completions


class ConnectivityError(Exception):
    """某个目标不可达/报错。消息可安全展示给用户（不含密钥）。"""


def validate_and_ping(credentials: dict) -> None:
    """解析 4 槽位配置，并向每个已配置目标发送一次最小 ping。

    :raises ConfigError: 路由配置不合法
    :raises ConnectivityError: 某个目标 ping 失败（不做失败转移，立即抛出）
    """
    policy, connect_s, read_s = load_policy_from_credentials(credentials)
    for target in policy.targets.values():
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
