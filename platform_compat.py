"""生产平台兼容垫片：在 dify_plugin 被导入之前规范化 INSTALL_METHOD 环境变量。

背景：部分 Dify 生产部署（K8s + 插件网关形态）会以 Lambda 风格调起插件，向插件
进程注入 `INSTALL_METHOD=aws_lambda`。而官方 dify-plugin SDK（截至 0.10.2 及其
主分支）的枚举只有 local / remote / serverless，未知取值会让 SDK 在 **import
阶段** 抛 pydantic ValidationError —— 插件进程启动即崩，用户侧表现为"保存凭据
失败"。（0.2.8 在生产环境实测复现。）

本模块不导入 dify_plugin，只负责环境规范化，供 main.py 在 import SDK 前调用：
  - 已知取值：统一为小写后原样透传；
  - 空取值：移除变量，让 SDK 使用默认值（local）；
  - 未知取值（如 aws_lambda）：映射为 serverless —— 即 SDK 在插件内起 HTTP
    服务（监听 SERVERLESS_HOST:SERVERLESS_PORT，默认 0.0.0.0:8080）接受调用，
    与 Lambda/网关式部署的语义一致；
  - 若平台实际使用 stdio（与 daemon 本地运行时相同的管道协议），部署侧设置
    `ROUTED_MODEL_INSTALL_METHOD=local`（或 remote/serverless）即可覆盖映射
    结果，无需重新打包插件。

规范化发生时向 stdout 输出一条与 SDK 插件日志同格式的 JSON 行（{"event":"log"…}），
可在 daemon 容器日志中确认映射生效；只含变量名与取值枚举，不含任何环境变量值。
"""

from __future__ import annotations

import json
import os
import sys
import time

_KNOWN = ("local", "remote", "serverless")


def _emit(message: str) -> None:
    try:
        sys.stdout.write(
            json.dumps(
                {"event": "log",
                 "data": {"level": "INFO", "message": message, "timestamp": time.time()}},
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.stdout.flush()
    except Exception:  # noqa: BLE001  # 诊断输出绝不允许影响启动
        pass


def normalize_install_method(environ: dict | None = None) -> str | None:
    """返回规范化后 SDK 将实际使用的 INSTALL_METHOD；未设置时返回 None。"""
    env = os.environ if environ is None else environ
    raw = env.get("INSTALL_METHOD")
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        del env["INSTALL_METHOD"]
        _emit("[routed_model] INSTALL_METHOD 为空，已移除以便 SDK 使用默认值 local")
        return None
    if value in _KNOWN:
        if value != raw:
            env["INSTALL_METHOD"] = value
            _emit(f"[routed_model] INSTALL_METHOD 规范化为 {value}")
        return value
    override = (env.get("ROUTED_MODEL_INSTALL_METHOD") or "").strip().lower()
    target = override if override in _KNOWN else "serverless"
    env["INSTALL_METHOD"] = target
    _emit(
        "[routed_model] INSTALL_METHOD 取值不是官方 SDK 支持的模式（多为 Lambda 风格部署注入），"
        f"已映射为 {target}；如需 stdio 请在部署侧设置 ROUTED_MODEL_INSTALL_METHOD=local"
    )
    return target
