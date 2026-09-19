import asyncio
import base64
import logging

import aiohttp
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


async def _download_image(url: str) -> bytes:
    """Скачивает готовую картинку, если API вернул ссылку, а не base64."""
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise LLMError(f"Images: не удалось скачать результат, HTTP {resp.status}")
                data = await resp.read()
    except aiohttp.ClientError as e:
        raise LLMError(f"Images: ошибка скачивания результата: {e}") from e
    if not data:
        raise LLMError("Images: скачанный результат пуст")
    return data


async def edit_image(image: bytes, prompt: str) -> bytes:
    """Новая версия изображения по фото и описанию (OpenAI-совместимый /images/edits)."""
    if not config.VIBECODE_API_KEY:
        raise LLMError("VIBECODE_API_KEY не задан — вызов API невозможен")
    if not config.IMAGE_MODEL:
        raise LLMError("IMAGE_MODEL не задан — генерация изображений выключена")
    # без повторов: генерация платная, лишние запросы не нужны
    client = _get_client().with_options(timeout=config.IMAGE_TIMEOUT, max_retries=0)
    try:
        response = await client.images.edit(
            model=config.IMAGE_MODEL,
            image=("photo.jpg", image, "image/jpeg"),
            prompt=prompt,
        )
    except APITimeoutError as e:
        raise LLMError(f"Images: превышен таймаут {config.IMAGE_TIMEOUT:.0f}s: {e}") from e
    except APIConnectionError as e:
        raise LLMError(f"Images: ошибка соединения с {config.LLM_BASE_URL}: {e}") from e
    except APIStatusError as e:
        raise LLMError(f"Images: HTTP {e.status_code}: {e.message}") from e

    if not response.data:
        raise LLMError("Images: ответ без данных")
    item = response.data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        try:
            return base64.b64decode(b64)
        except ValueError as e:
            raise LLMError(f"Images: некорректный base64 в ответе: {e}") from e
    url = getattr(item, "url", None)
    if url:
        return await _download_image(url)
    raise LLMError("Images: в ответе нет ни b64_json, ни url")


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
