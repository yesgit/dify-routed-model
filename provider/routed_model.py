from dify_plugin import ModelProvider

from policy import ConfigError, load_policy_from_credentials
from targets import chat_completions


class RoutedModelProvider(ModelProvider):
    def validate_provider_credentials(self, credentials: dict) -> None:
        """保存/添加自定义模型实例时调用：先校验配置，再对每个目标发起一次最小对话请求。

        注意：ping 会真实打到上游并消耗少量 token，这是方案 §4 明确要求的连通性验收。
        """
        try:
            policy, api_keys, connect_s, read_s = load_policy_from_credentials(credentials)
        except ConfigError as e:
            raise ValueError(f"路由配置无效：{e}") from None

        for target in policy.targets.values():
            payload = {
                "model": target.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "stream": False,
            }
            try:
                resp = chat_completions(
                    target, api_keys.get(target.credential_ref, ""), payload,
                    timeout=(connect_s, min(read_s, 30)), stream=False,
                )
            except Exception as e:  # 包括 UpstreamError 与 requests 异常
                raise ValueError(f"目标 {target.id} 连通性校验失败：{e}") from None
            try:
                resp.json()
            except ValueError:
                pass  # 200 但非 JSON：视为可达
            finally:
                resp.close()
