import asyncio
import logging

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

import config

log = logging.getLogger(__name__)

LLM_TIMEOUT = 60.0

_client: AsyncOpenAI | None = None


class LLMError(RuntimeError):
    """Единая ошибка вызова LLM — детали не глотаются, всегда пробрасываются."""


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not config.VIBECODE_API_KEY:
            raise LLMError("VIBECODE_API_KEY не задан — вызов LLM невозможен")
        _client = AsyncOpenAI(
            api_key=config.VIBECODE_API_KEY,
            base_url=config.LLM_BASE_URL,
            timeout=LLM_TIMEOUT,
        )
    return _client


async def call_llm(messages: list[dict]) -> str:
    if not config.VIBECODE_API_KEY:
        raise LLMError("VIBECODE_API_KEY не задан — вызов LLM невозможен")
    try:
        response = await _get_client().chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
        )
    except APITimeoutError as e:
        raise LLMError(f"LLM: превышен таймаут {LLM_TIMEOUT:.0f}s: {e}") from e
    except APIConnectionError as e:
        raise LLMError(f"LLM: ошибка соединения с {config.LLM_BASE_URL}: {e}") from e
    except APIStatusError as e:
        raise LLMError(f"LLM: HTTP {e.status_code}: {e.message}") from e

    if not response.choices:
        raise LLMError("LLM: ответ без choices")
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise LLMError("LLM: пустой текст ответа")
    return content


async def test_llm() -> str:
    return await call_llm(
        [
            {"role": "system", "content": "Ты — тестовый помощник."},
            {
                "role": "user",
                "content": "Ответь ровно одной фразой: GPT-5.6 Terra работает.",
            },
        ]
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        answer = asyncio.run(test_llm())
    except LLMError as e:
        print(f"LLM TEST FAILED: {e}")
        raise SystemExit(1) from e
    print("LLM TEST OK")
    print(f"Model: {config.LLM_MODEL}")
    print(f"Answer: {answer}")
