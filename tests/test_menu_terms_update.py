"""Проверки нового названия игры и сокращённых условий в обоих языках."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.utils.i18n import I18n

from config_data.config import ROOT
from handlers import navigation, other


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_games_menu_has_new_label_and_original_routes(
    language: str,
) -> None:
    """Меню сохраняет приглашение, WebApp и возврат, но скрывает вторую игру."""
    i18n = I18n(path=str(ROOT / "locales"), domain="bot_00_template")
    bot = SimpleNamespace(
        test_mode=True, wordli_webapp_url="https://example.invalid/wordli",
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": language}),
        set_state=AsyncMock(), set_data=AsyncMock(),
    )
    message = SimpleNamespace(answer=AsyncMock(), bot=bot)

    with i18n.context(), i18n.use_locale(language):
        await navigation.open_words(message, state, bot)

    invitation = message.answer.await_args_list[-1]
    buttons = invitation.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in buttons] == [1, 1]
    assert invitation.args[0] == (
        "Which game shall we play?\n\n"
        "(Due to the game's mechanics, it is currently available only in Russian)"
        if language == "en" else "В какую игру сыграем?"
    )
    assert buttons[0][0].text == "5БУКВ"
    assert buttons[0][0].web_app.url == bot.wordli_webapp_url
    assert buttons[1][0].callback_data == "wordli_back_to_main"
    assert buttons[1][0].text == (
        "Back to main ↩️" if language == "en" else "Назад на главную ↩️"
    )


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_terms_have_six_ordered_localized_points(language: str) -> None:
    """Обработчик и собранные переводы содержат ровно согласованные пункты."""
    i18n = I18n(path=str(ROOT / "locales"), domain="bot_00_template")
    message = SimpleNamespace(
        delete=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=55)),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=9000000001), message=message,
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}), update_data=AsyncMock(),
    )

    with i18n.context(), i18n.use_locale(language):
        await other.terms_cmd(callback, state)

    text = message.answer.await_args_list[-1].args[0]
    paragraphs = text.strip().split("\n\n")
    assert paragraphs[0] == (
        "Условия использования @Terminatorvan_bot:" if language == "ru"
        else "Terms of use @Terminatorvan_bot:"
    )
    assert len(paragraphs) == 7
    assert [paragraph[:2] for paragraph in paragraphs[1:]] == [
        "1.", "2.", "3.", "4.", "5.", "6.",
    ]
    if language == "ru":
        assert paragraphs[4] == (
            "4. Рецепты из Кулинарной книги проверены на съедобность."
        )
        assert paragraphs[5].startswith("5. Прогноз погоды и курсы валют")
        assert paragraphs[6].startswith("6. Все донаты добровольные.")
        assert "кулинарные эксперименты" not in text
        assert "Калькулятор пиццы" not in text
        assert "В случае сбоев" not in text
    else:
        assert paragraphs[4] == (
            "4. The recipes from the Cookbook have been tested for edibility."
        )
        assert paragraphs[5].startswith("5. Weather forecasts and currency rates")
        assert paragraphs[6].startswith("6. All donations are voluntary.")
        assert "culinary experiments" not in text
        assert "pizza calculator" not in text
        assert "If there are failures" not in text
        assert "Этот бот" not in text
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "back_to_info"
    assert markup.inline_keyboard[1][0].callback_data == "about_back_to_main"
    state.update_data.assert_awaited_once_with(last_message_id=55)
