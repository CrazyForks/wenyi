"""根据配置创建内置 LLM provider。"""

from __future__ import annotations

from ..config import Config
from .base import LLMClient


def build_client(config: Config) -> LLMClient:
    provider = config.llm.provider.lower()
    if provider == "deepseek":
        from .providers.deepseek import DeepSeekClient

        return DeepSeekClient(config.llm)
    if provider == "fake":
        from .providers.fake import FakeClient

        return FakeClient()
    raise ValueError(f"未知 provider：{provider}（支持 deepseek / fake）")
