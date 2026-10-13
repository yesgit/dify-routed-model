# 生产平台兼容：必须在导入 dify_plugin 之前规范化 INSTALL_METHOD。
# 某些生产部署注入 aws_lambda 等官方 SDK 不识别的取值，SDK 会在 import 阶段
# 直接校验失败（插件启动即崩，表现为保存凭据失败）。映射规则与覆盖开关见
# platform_compat.py。
from platform_compat import normalize_install_method

normalize_install_method()

from dify_plugin import DifyPluginEnv, Plugin  # noqa: E402

plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=600))

if __name__ == "__main__":
    plugin.run()
