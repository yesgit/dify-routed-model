from dify_plugin import ModelProvider

from policy import ConfigError
from validation import ConnectivityError, validate_and_ping


class RoutedModelProvider(ModelProvider):
    def validate_provider_credentials(self, credentials: dict) -> None:
        """保存供应商级配置时调用（与自定义模型实例校验同一实现，见 validation.py）。

        注意：ping 会真实打到上游并消耗少量 token，这是配置验收的一部分。
        """
        try:
            validate_and_ping(credentials)
        except ConfigError as e:
            raise ValueError(f"路由配置无效：{e}") from None
        except ConnectivityError as e:
            raise ValueError(str(e)) from None
