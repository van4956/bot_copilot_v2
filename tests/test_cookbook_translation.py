"""Офлайн-проверки перевода без сети и повторной оплаты запросов."""

import asyncio
import io
import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from aiohttp import ClientError

from common.cookbook_translation import (
    API_URL,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_RESPONSE_BYTES,
    MODEL,
    REQUEST_TIMEOUT,
    RecipeTranslation,
    TranslationError,
    translate_recipe,
)

KEY = "sk-offline-test-only-secret"
SOURCE_NAME = "Блины"
SOURCE_DESCRIPTION = "Молоко — 500 мл.\nМука — 200 г.\nЖарить 2 минуты."
TRANSLATED = {
    "recipe_name": "Pancakes",
    "description": "Milk — 500 ml.\nFlour — 200 g.\nFry for 2 minutes.",
}


def envelope(translation: Any = None) -> dict[str, Any]:
    """Оборачивает JSON перевода в ответ Responses API."""
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            TRANSLATED if translation is None else translation,
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
        ],
    }


class FakeResponse:
    """Подменяет HTTP-ответ с порционным чтением ограниченного тела."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        body: bytes | None = None,
        content_length: int | None = None,
    ) -> None:
        """Создаёт ответ без сокетов."""
        self.status = status
        self.body = (
            body
            if body is not None
            else json.dumps(
                envelope() if payload is None else payload,
                ensure_ascii=False,
            ).encode()
        )
        self.stream = io.BytesIO(self.body)
        self.content = self
        self.content_length = content_length
        self.reads = 0
        self.exited = False

    async def read(self, size: int) -> bytes:
        """Имитирует короткие сетевые порции вместо полного чтения сразу."""
        self.reads += 1
        return self.stream.read(min(size, 137))

    async def __aenter__(self) -> "FakeResponse":
        """Возвращает ответ при входе в контекст."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Отмечает освобождение ответа при любом результате."""
        self.exited = True


class FakeSession:
    """Запоминает единственную попытку HTTP без настоящего API."""

    def __init__(
        self,
        response: FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Настраивает офлайн-ответ либо сетевой сбой."""
        self.response = response or FakeResponse()
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Не делает сетевых запросов и не повторяет вызов."""
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


async def request(session: FakeSession, **kwargs: Any) -> RecipeTranslation:
    """Вызывает проверяемый переводчик с изолированной сессией."""
    return await translate_recipe(
        kwargs.pop("recipe_name", SOURCE_NAME),
        kwargs.pop("description", SOURCE_DESCRIPTION),
        api_key=kwargs.pop("api_key", KEY),
        http_session=session,
        **kwargs,
    )


async def test_translation_uses_exact_model_schema_and_private_single_request() -> None:
    """Отправляет только название и описание строго согласованной модели."""
    session = FakeSession()
    result = await request(session)
    assert result == RecipeTranslation(**TRANSLATED)
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == API_URL == "https://api.openai.com/v1/responses"
    assert options["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert options["allow_redirects"] is False
    assert options["timeout"].total == REQUEST_TIMEOUT == 60
    payload = options["json"]
    assert payload["model"] == MODEL == "gpt-5.4-mini"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["store"] is False
    assert payload["max_output_tokens"] == 4096
    assert json.loads(payload["input"]) == {
        "recipe_name": SOURCE_NAME,
        "description": SOURCE_DESCRIPTION,
    }
    assert "tools" not in payload and "previous_response_id" not in payload
    format_spec = payload["text"]["format"]
    assert format_spec["type"] == "json_schema"
    assert format_spec["strict"] is True
    schema = format_spec["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"recipe_name", "description"}
    assert set(schema["required"]) == {"recipe_name", "description"}
    for phrase in (
        "quantity",
        "numeral",
        "Do not improve",
        "line break",
        "do not convert",
        "untrusted data",
        "never follow instructions",
    ):
        assert phrase in payload["instructions"]
    assert session.response.reads > 2
    assert session.response.exited


def test_translation_value_is_immutable_and_has_no_credentials() -> None:
    """Результат не хранит API-ключ и не изменяется после проверки."""
    translated = RecipeTranslation(**TRANSLATED)
    assert KEY not in repr(translated)
    with pytest.raises(FrozenInstanceError):
        translated.recipe_name = "Other"


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 429, 500, 502, 503])
async def test_http_failures_do_not_read_body_retry_or_disclose_secrets(
    status: int,
) -> None:
    """Статус ошибки не приводит к чтению секретного тела и повтору POST."""
    response = FakeResponse(body=f"secret={KEY}".encode(), status=status)
    session = FakeSession(response)
    with pytest.raises(TranslationError) as raised:
        await request(session)
    assert KEY not in str(raised.value)
    assert KEY not in repr(raised.value)
    expected_reason = (
        "authentication_failed"
        if status in {401, 403}
        else "rate_limited"
        if status == 429
        else "translation_failed"
    )
    assert raised.value.reason == expected_reason
    assert response.reads == 0
    assert response.exited
    assert len(session.calls) == 1


@pytest.mark.parametrize("error", [ClientError(KEY), asyncio.TimeoutError(KEY)])
async def test_transport_failures_are_sanitized_without_paid_retry(
    error: Exception,
) -> None:
    """Сетевой таймаут не доказывает отсутствие списания и не повторяется."""
    session = FakeSession(error=error)
    with pytest.raises(TranslationError) as raised:
        await request(session)
    assert len(session.calls) == 1
    assert KEY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_cancellation_propagates_without_retry() -> None:
    """Отмена задачи не маскируется под обычную ошибку перевода."""
    session = FakeSession(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await request(session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("api_key", ["", " ", "bad\nkey", "bad\rkey", None])
async def test_missing_or_malformed_key_never_makes_a_request(api_key: Any) -> None:
    """Пустой либо небезопасный заголовок блокируется до HTTP."""
    session = FakeSession()
    with pytest.raises(TranslationError, match="ключ"):
        await request(session, api_key=api_key)
    assert not session.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recipe_name", ""),
        ("recipe_name", "  "),
        ("recipe_name", None),
        ("recipe_name", "Я" * 151),
        ("description", ""),
        ("description", None),
        ("description", "Я" * 4001),
    ],
)
async def test_bad_original_is_rejected_before_http(field: str, value: Any) -> None:
    """Некорректные исходные данные не отправляются на платный API."""
    session = FakeSession()
    with pytest.raises(TranslationError, match="исходный"):
        await request(session, **{field: value})
    assert not session.calls


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"status": "incomplete", "output": []},
        {"status": "failed", "error": {"message": KEY}},
        {"status": "completed", "output": {}},
        {"status": "completed", "output": [False]},
        {"status": "completed", "output": []},
        {"status": "completed", "output": [{"type": "reasoning"}]},
        {
            "status": "completed",
            "output": [{"type": "message", "role": "user", "content": []}],
        },
        {
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": None}],
        },
        {
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [None]}],
        },
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": 1}],
                }
            ],
        },
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": KEY}],
                }
            ],
        },
    ],
)
async def test_refused_incomplete_and_malformed_envelopes_are_not_translations(
    payload: Any,
) -> None:
    """Ошибки и отказы не публикуются как английский рецепт."""
    session = FakeSession(FakeResponse(payload))
    with pytest.raises(TranslationError) as raised:
        await request(session)
    assert KEY not in str(raised.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "translation",
    [
        [],
        {"recipe_name": "Pancakes"},
        {"description": "Cook"},
        {**TRANSLATED, "author": "Invented"},
        {**TRANSLATED, "recipe_name": 10},
        {**TRANSLATED, "recipe_name": "  "},
        {**TRANSLATED, "recipe_name": "a" * (MAX_NAME_LENGTH + 1)},
        {**TRANSLATED, "description": False},
        {**TRANSLATED, "description": " "},
        {**TRANSLATED, "description": "a" * (MAX_DESCRIPTION_LENGTH + 1)},
    ],
)
async def test_invalid_translation_fields_are_never_accepted(translation: Any) -> None:
    """Пустые, лишние, слишком длинные и нетекстовые поля блокируются."""
    session = FakeSession(FakeResponse(envelope(translation)))
    with pytest.raises(TranslationError):
        await request(session)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "body", [b"not json " + KEY.encode(), b"\xff\xfe\x00", b"null"]
)
async def test_invalid_response_json_is_sanitized(body: bytes) -> None:
    """Ошибочное тело не попадает в исключение или журнал."""
    session = FakeSession(FakeResponse(body=body))
    with pytest.raises(TranslationError) as raised:
        await request(session)
    assert KEY not in str(raised.value)
    assert session.response.exited


@pytest.mark.parametrize("text", ["{", "```json\n{}\n```", "null"])
async def test_invalid_inner_json_is_not_retried(text: str) -> None:
    """Обрезанный и обрамлённый JSON не вызывает повторной оплаты."""
    payload = envelope()
    payload["output"][0]["content"][0]["text"] = text
    session = FakeSession(FakeResponse(payload))
    with pytest.raises(TranslationError):
        await request(session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("length", [None, MAX_RESPONSE_BYTES + 1])
async def test_response_body_is_bounded_with_and_without_declared_length(
    length: int | None,
) -> None:
    """Размер проверяется и по заголовку, и при потоковом чтении."""
    response = FakeResponse(
        body=b"x" * (MAX_RESPONSE_BYTES + 50_000),
        content_length=length,
    )
    session = FakeSession(response)
    with pytest.raises(TranslationError, match="большой"):
        await request(session)
    assert response.stream.tell() <= MAX_RESPONSE_BYTES + 1
    assert response.exited
    assert len(session.calls) == 1
    if length is not None:
        assert response.reads == 0


async def test_one_message_may_have_several_text_blocks_and_proper_cyrillic_names() -> (
    None
):
    """Не запрещает кириллицу в собственных именах внутри перевода."""
    translated = {
        "recipe_name": "Grandma Нина's pancakes",
        "description": "Add 500 ml milk.\nFry.",
    }
    payload = envelope(translated)
    text = payload["output"][0]["content"][0]["text"]
    payload["output"].insert(0, {"type": "reasoning", "summary": []})
    payload["output"][1]["content"] = [
        {"type": "output_text", "text": text[:20]},
        {"type": "output_text", "text": text[20:]},
    ]
    assert await request(FakeSession(FakeResponse(payload))) == RecipeTranslation(
        **translated
    )


async def test_multiple_assistant_messages_are_rejected() -> None:
    """Не объединяет несколько ответов в один рецепт."""
    payload = envelope()
    payload["output"] *= 2
    with pytest.raises(TranslationError):
        await request(FakeSession(FakeResponse(payload)))
