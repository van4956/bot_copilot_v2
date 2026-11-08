"""Сердечко ставится только на успешно отправленные архивные фото."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SetMessageReaction
from aiogram.types import FSInputFile, Message, ReactionTypeEmoji

from handlers import private


@pytest.fixture
def cat_message(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Изолирует фото, реакции, каталог и внешнюю сеть."""
    monkeypatch.setattr(private, "_", lambda text: text)
    monkeypatch.setattr(private.r, "random", lambda: 0.1)
    monkeypatch.setattr(private.r, "choice", lambda images: images[0])
    monkeypatch.setattr(private.os, "listdir", lambda path: ["archive.jpg"])
    monkeypatch.setattr(
        private,
        "get_json",
        AsyncMock(
            return_value=[
                {"url": "https://example.invalid/external.jpg"},
            ]
        ),
    )
    sent = SimpleNamespace(react=AsyncMock())
    return SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(return_value=sent),
        answer_document=AsyncMock(return_value=sent),
        bot=SimpleNamespace(http_session=object()),
    )


async def test_archived_photo_receives_one_native_heart(
    cat_message: SimpleNamespace,
) -> None:
    """Реакция относится к ответу с архивным фото, а не к входящему сообщению."""
    await private.cat_cmd(cat_message, {})

    cat_message.answer_photo.assert_awaited_once()
    photo = cat_message.answer_photo.await_args.args[0]
    assert isinstance(photo, FSInputFile)
    assert str(photo.path).replace("\\", "/") == "common/image_cat/archive.jpg"
    sent = cat_message.answer_photo.return_value
    sent.react.assert_awaited_once_with([ReactionTypeEmoji(emoji="❤")])
    private.get_json.assert_not_awaited()
    cat_message.answer_document.assert_not_awaited()
    cat_message.answer.assert_not_awaited()


async def test_native_reaction_targets_returned_photo_message(
    monkeypatch: pytest.MonkeyPatch,
    cat_message: SimpleNamespace,
) -> None:
    """Настоящий объект Message адресует реакцию отправленной фотографии."""
    bot = Bot("123456:OFFLINE_TEST_ONLY")
    request = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "__call__", request)
    sent = Message.model_validate(
        {
            "message_id": 456,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 9000000001, "type": "private"},
        }
    ).as_(bot)
    cat_message.answer_photo.return_value = sent
    try:
        await private.cat_cmd(cat_message, {})
        request.assert_awaited_once()
        method = request.await_args.args[0]
        assert isinstance(method, SetMessageReaction)
        assert method.chat_id == sent.chat.id
        assert method.message_id == sent.message_id
        assert method.reaction == [ReactionTypeEmoji(emoji="❤")]
        private.get_json.assert_not_awaited()
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    "extension,method",
    [
        ("jpg", "answer_photo"),
        ("png", "answer_photo"),
        ("gif", "answer_document"),
    ],
)
async def test_external_image_has_no_automatic_reaction(
    monkeypatch: pytest.MonkeyPatch,
    cat_message: SimpleNamespace,
    extension: str,
    method: str,
) -> None:
    """Обычная ветка внешнего сервиса не ставит реакций ни фото, ни документу."""
    monkeypatch.setattr(private.r, "random", lambda: 0.2)
    url = f"https://example.invalid/external.{extension}"
    private.get_json.return_value = [{"url": url}]

    await private.cat_cmd(cat_message, {})

    getattr(cat_message, method).assert_awaited_once_with(url)
    private.get_json.assert_awaited_once()
    cat_message.answer_photo.return_value.react.assert_not_awaited()
    cat_message.answer.assert_not_awaited()


async def test_empty_archive_falls_back_without_reaction(
    monkeypatch: pytest.MonkeyPatch,
    cat_message: SimpleNamespace,
) -> None:
    """Пустой архив сохраняет прежний переход к внешнему котику без сердечка."""
    monkeypatch.setattr(private.os, "listdir", lambda path: [])

    await private.cat_cmd(cat_message, {})

    cat_message.answer.assert_awaited_once_with("Локальные изображения не найдены.")
    cat_message.answer_photo.assert_awaited_once_with(
        "https://example.invalid/external.jpg"
    )
    cat_message.answer_photo.return_value.react.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(
            method=SetMessageReaction(chat_id=1, message_id=2),
            message="SECRET_TOKEN_REACTION_FAILURE",
        ),
        ClientError("SECRET_TOKEN_REACTION_FAILURE"),
        asyncio.TimeoutError("SECRET_TOKEN_REACTION_FAILURE"),
        TimeoutError("SECRET_TOKEN_REACTION_FAILURE"),
    ],
)
async def test_reaction_error_keeps_sent_photo_without_fallback_or_duplicate(
    cat_message: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """Сбой реакции не отменяет фото и не раскрывает ответ Telegram в журнале."""
    cat_message.answer_photo.return_value.react.side_effect = error

    await private.cat_cmd(cat_message, {})

    cat_message.answer_photo.assert_awaited_once()
    cat_message.answer_photo.return_value.react.assert_awaited_once()
    cat_message.answer.assert_not_awaited()
    cat_message.answer_document.assert_not_awaited()
    private.get_json.assert_not_awaited()
    assert "Не удалось добавить реакцию к архивному фото" in caplog.text
    assert "SECRET_TOKEN_REACTION_FAILURE" not in caplog.text


async def test_failed_photo_send_does_not_attempt_reaction(
    cat_message: SimpleNamespace,
) -> None:
    """Сердечко не отправляется, если сообщения с фото ещё нет."""
    sent = cat_message.answer_photo.return_value
    error = TelegramAPIError(
        method=SetMessageReaction(chat_id=1, message_id=2),
        message="Photo sending failed",
    )
    cat_message.answer_photo.side_effect = error

    with pytest.raises(TelegramAPIError, match="Photo sending failed"):
        await private.cat_cmd(cat_message, {})

    sent.react.assert_not_awaited()
    cat_message.answer_photo.assert_awaited_once()
    cat_message.answer_document.assert_not_awaited()
    private.get_json.assert_not_awaited()
