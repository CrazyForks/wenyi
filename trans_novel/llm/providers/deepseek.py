"""通过 OpenAI 兼容接口调用 DeepSeek。"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
from ..tiers import resolve_tier

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"


class DeepSeekTierOptions(BaseModel):
    """DeepSeek 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = True
    reasoning_effort: str = "high"


class DeepSeekTierConfig(BaseModel):
    """DeepSeek provider 已补全并校验的运行时档位配置。"""

    model: str
    options: DeepSeekTierOptions = Field(default_factory=DeepSeekTierOptions)


def _default_tiers() -> dict[str, DeepSeekTierConfig]:
    """返回 DeepSeek 当前默认模型档位；每个客户端持有独立配置。"""
    return {
        "strong": DeepSeekTierConfig(
            model="deepseek-v4-pro",
        ),
        "cheap": DeepSeekTierConfig(
            model="deepseek-v4-flash",
        ),
        "fast": DeepSeekTierConfig(
            model="deepseek-v4-flash",
            options=DeepSeekTierOptions(thinking=False),
        ),
    }


def _resolve_tiers(
    overrides: dict[str, TierConfig],
) -> dict[str, DeepSeekTierConfig]:
    """把通用用户覆盖合并进 DeepSeek 默认档位，并校验专属 options。"""
    tiers = _default_tiers()
    for name, override in overrides.items():
        current = tiers.get(name)
        model = override.model or (current.model if current else None)
        if not model:
            raise ValueError(f"llm.tiers.{name}.model 不能为空")
        option_values = current.options.model_dump() if current else {}
        option_values.update(override.options)
        tiers[name] = DeepSeekTierConfig(
            model=model,
            options=DeepSeekTierOptions.model_validate(option_values),
        )
    return tiers


class DeepSeekClient(LLMClient):
    def __init__(self, cfg: LLMConfig):
        super().__init__()
        self.cfg = cfg
        self.base_url = cfg.base_url or DEFAULT_BASE_URL
        self.api_key_env = cfg.api_key_env or DEFAULT_API_KEY_ENV
        self.tiers = _resolve_tiers(cfg.tiers)
        self._client = None  # 惰性创建
        self._client_lock = threading.Lock()  # 预扫并行时防惰性初始化竞态

    def _ensure_client(self):
        with self._client_lock:
            return self._ensure_client_locked()

    def _ensure_client_locked(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "需要 openai SDK：pip install openai（或把 llm.provider 设为 fake 做离线测试）"
                ) from error
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"未设置环境变量 {self.api_key_env}（DeepSeek API key）"
                )
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.cfg.timeout,
            )
        return self._client

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        tier_config = resolve_tier(self.tiers, tier)
        client = self._ensure_client()

        kwargs: dict[str, Any] = {
            "model": tier_config.model,
            "messages": messages,
            "stream": False,
        }
        if tier_config.options.thinking:
            kwargs["reasoning_effort"] = tier_config.options.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            # DeepSeek thinking 模式下 max_tokens 含推理 token（总输出上限）。
            # 带紧上限的调用若经回退链落到 thinking 档，抬到安全下限防推理被截断。
            kwargs["max_tokens"] = (
                max(max_tokens, 4096)
                if tier_config.options.thinking
                else max_tokens
            )

        # 网络/限流/超时 → tenacity 指数退避重试（最多 max_retries 次重试）
        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> str:
            response = client.chat.completions.create(**kwargs)
            self.usage.record(tier, getattr(response, "usage", None), stage)
            return response.choices[0].message.content or ""

        return _call()
