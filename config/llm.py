"""litellm 调用统一入口，自动注入 api_base / api_key 配置"""
from __future__ import annotations

import litellm

from config.settings import settings


async def llm_complete(
    messages: list[dict],
    model: str = "",
    max_tokens: int = 8192,
) -> str:
    """统一 LLM 调用入口，自动处理 OpenAI 兼容接口（如智谱）的 api_base 注入。"""
    call_kwargs: dict = {}
    if settings.OPENAI_API_BASE:
        call_kwargs["api_base"] = settings.OPENAI_API_BASE
        call_kwargs["api_key"] = settings.OPENAI_API_KEY

    response = await litellm.acompletion(
        model=model or settings.LLM_MODEL,
        max_tokens=max_tokens,
        messages=messages,
        **call_kwargs,
    )
    return response.choices[0].message.content
