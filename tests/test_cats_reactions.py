"""Эффект сообщения и обычное сердечко относятся только к архивным фото."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SendPhoto, SetMessageReaction
from aiogram.types import FSInputFile, Message, ReactionTypeEmoji

from handlers import private


@pytest.fixture
def cat_message(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Изолирует фото, реакции, каталог и внешнюю сеть."""
    monkeypatch.setattr(private, "_", lambda text: text)
    monkeypatch.setattr(private.asyncio, "sleep", AsyncMock())
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


async def test_archived_photo_has_message_effect_and_one_native_heart(
    cat_message: SimpleNamespace,
) -> None:
    """Реакция относится к ответу с архивным фото, а не к входящему сообщению."""
    await private.cat_cmd(cat_message, {})

    cat_message.answer_photo.assert_awaited_once()
    photo = cat_message.answer_photo.await_args.args[0]
    assert isinstance(photo, FSInputFile)
    assert str(photo.path).replace("\\", "/") == "common/image_cat/archive.jpg"
    assert cat_message.answer_photo.await_args.kwargs == {
        "message_effect_id": private.CAT_HEART_EFFECT_ID
    }
    sent = cat_message.answer_photo.return_value
    private.asyncio.sleep.assert_not_awaited()
    sent.react.assert_awaited_once_with([ReactionTypeEmoji(emoji="❤")])
    private.get_json.assert_not_awaited()
    cat_message.answer_document.assert_not_awaited()
    cat_message.answer.assert_not_awaited()


async def test_message_effect_is_sent_with_photo_without_delay_or_buttons(
    cat_message: SimpleNamespace,
) -> None:
    """Эффект передаётся при отправке фото; реакции не предшествует пауза."""
    events: list[str] = []
    sent = cat_message.answer_photo.return_value

    async def send_photo(photo: FSInputFile, **kwargs: object) -> SimpleNamespace:
        """Проверяет эффект в единственной отправке фотографии."""
        assert kwargs == {"message_effect_id": private.CAT_HEART_EFFECT_ID}
        events.append("photo")
        return sent

    async def react(*args: object, **kwargs: object) -> None:
        """Отмечает обычную реакцию после отправки фотографии."""
        events.append("reaction")

    cat_message.answer_photo.side_effect = send_photo
    sent.react.side_effect = react

    await private.cat_cmd(cat_message, {})

    assert events == ["photo", "reaction"]
    private.asyncio.sleep.assert_not_awaited()
    cat_message.answer_photo.assert_awaited_once()
    sent.react.assert_awaited_once()


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
        assert method.is_big is None
        private.asyncio.sleep.assert_not_awaited()
        private.get_json.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_native_photo_request_contains_message_effect(
    monkeypatch: pytest.MonkeyPatch,
    cat_message: SimpleNamespace,
) -> None:
    """Настоящий метод aiogram передаёт эффект оригинальному фото без кнопок."""
    bot = Bot("123456:OFFLINE_TEST_ONLY")
    sent = cat_message.answer_photo.return_value
    request = AsyncMock(return_value=sent)
    monkeypatch.setattr(Bot, "__call__", request)
    incoming = Message.model_validate(
        {
            "message_id": 123,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 9000000001, "type": "private"},
        }
    ).as_(bot)
    try:
        await private.cat_cmd(incoming, {})
        request.assert_awaited_once()
        method = request.await_args.args[0]
        assert isinstance(method, SendPhoto)
        assert isinstance(method.photo, FSInputFile)
        assert method.message_effect_id == private.CAT_HEART_EFFECT_ID
        assert method.reply_markup is None
        sent.react.assert_awaited_once_with([ReactionTypeEmoji(emoji="❤")])
        private.asyncio.sleep.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_rejected_effect_falls_back_to_same_photo_without_effect(
    cat_message: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Только явный отказ эффекта позволяет повторить неотправленное фото."""
    sent = cat_message.answer_photo.return_value
    error = TelegramBadRequest(
        method=SendPhoto(chat_id=1, photo="synthetic"),
        message="Bad Request: MESSAGE_EFFECT_ID_INVALID SECRET_TOKEN_FAILURE",
    )
    cat_message.answer_photo.side_effect = [error, sent]

    await private.cat_cmd(cat_message, {})

    assert cat_message.answer_photo.await_count == 2
    first, second = cat_message.answer_photo.await_args_list
    assert first.args == second.args
    assert first.kwargs == {"message_effect_id": private.CAT_HEART_EFFECT_ID}
    assert second.kwargs == {}
    sent.react.assert_awaited_once_with([ReactionTypeEmoji(emoji="❤")])
    private.asyncio.sleep.assert_not_awaited()
    private.get_json.assert_not_awaited()
    cat_message.answer.assert_not_awaited()
    assert "SECRET_TOKEN_FAILURE" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(
            method=SendPhoto(chat_id=1, photo="synthetic"),
            message="Bad Request: PHOTO_INVALID",
        ),
        ClientError("Unknown photo delivery"),
        asyncio.TimeoutError("Unknown photo delivery"),
    ],
)
async def test_unrelated_or_uncertain_photo_error_is_not_retried(
    cat_message: SimpleNamespace,
    error: Exception,
) -> None:
    """Ошибки файла и неопределённая доставка не создают повторных фотографий."""
    cat_message.answer_photo.side_effect = error
    with pytest.raises(type(error)):
        await private.cat_cmd(cat_message, {})
    cat_message.answer_photo.assert_awaited_once()
    cat_message.answer_photo.return_value.react.assert_not_awaited()
    private.get_json.assert_not_awaited()
    cat_message.answer.assert_not_awaited()


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
    private.asyncio.sleep.assert_not_awaited()
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
    private.asyncio.sleep.assert_not_awaited()


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
    private.asyncio.sleep.assert_not_awaited()
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
    private.asyncio.sleep.assert_not_awaited()
    cat_message.answer_photo.assert_awaited_once()
    cat_message.answer_document.assert_not_awaited()
    private.get_json.assert_not_awaited()


async def test_cancellation_during_photo_send_does_not_react_or_duplicate(
    cat_message: SimpleNamespace,
) -> None:
    """Отмена отправки фото не вызывает повторную доставку или реакцию."""
    cat_message.answer_photo.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await private.cat_cmd(cat_message, {})

    cat_message.answer_photo.assert_awaited_once()
    cat_message.answer_photo.return_value.react.assert_not_awaited()
    cat_message.answer.assert_not_awaited()
    cat_message.answer_document.assert_not_awaited()
    private.get_json.assert_not_awaited()
