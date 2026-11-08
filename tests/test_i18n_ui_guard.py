"""Ограниченная AST-защита прямых подписей Telegram от обхода gettext."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_HANDLERS = (
    "start",
    "private",
    "navigation",
    "other",
    "weather",
    "currency",
    "cookbook",
    "donate",
    "admin",
    "group",
    "owner",
)
TEXT_METHODS = {
    "answer",
    "reply",
    "edit_text",
    "edit_caption",
    "answer_poll",
    "reply_poll",
}
TEXT_METHODS_WITH_CHAT = {"send_message", "edit_message_text", "send_poll"}
KEYBOARD_FACTORIES = {"get_callback_btns", "get_url_btns", "get_inlineMix_btns"}
UI_CONSTRUCTORS = {
    "InlineKeyboardButton",
    "KeyboardButton",
    "LabeledPrice",
    "InputMediaPhoto",
    "InputMediaVideo",
    "InputMediaAnimation",
    "InputMediaAudio",
    "InputMediaDocument",
    "ForceReply",
    "ReplyKeyboardMarkup",
    "SendMessage",
    "SendPhoto",
    "SendInvoice",
    "EditMessageText",
    "EditMessageCaption",
}
VISIBLE_KEYWORDS = {
    "text",
    "caption",
    "title",
    "description",
    "label",
    "question",
    "explanation",
    "input_field_placeholder",
}
TRANSLATORS = {"_", "__", "gettext", "lazy_gettext", "ngettext", "pgettext"}
CURRENCY_CODES = {
    "USD",
    "RUB",
    "GEL",
    "EUR",
    "KZT",
    "TRY",
    "JPY",
    "VND",
    "CNY",
    "ILS",
    "PHP",
    "AED",
    "XTR",
}
UNCHANGED_LABELS = {
    # Названия продуктов, внешних сервисов и обозначение технического ping.
    "5БУКВ",
    "4ПО5",
    "Telegram",
    "Linkedin",
    "LinkedIn",
    "GitHub",
    "Kaggle",
    "ping-",
}
SERVICE_FUNCTIONS = {
    ("app.py", "notify_service"),
    ("handlers/start.py", "_notify_service"),
}


def _human_text(value: str) -> bool:
    """Отделяет переводимый текст от чисел, валют, ссылок и известных имён."""
    plain = re.sub(r"<[^>]*>", "", value).strip()
    if plain in CURRENCY_CODES | UNCHANGED_LABELS:
        return False
    if re.fullmatch(r"https?://\S+", plain):
        return False
    return re.search(r"[A-Za-zА-Яа-яЁё]", plain) is not None


def _direct_texts(node: ast.AST) -> list[str]:
    """Находит буквальный текст выражения, не раскрывая пользовательские данные."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if _human_text(node.value) else []
    if isinstance(node, ast.JoinedStr):
        text = "".join(
            item.value
            for item in node.values
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        return [text] if _human_text(text) else []
    if isinstance(node, ast.Call):
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if name in TRANSLATORS:
            return []
        if isinstance(node.func, ast.Attribute) and name in {
            "format",
            "strip",
            "upper",
            "lower",
        }:
            return _direct_texts(node.func.value)
        if name in {"escape", "str"}:
            return [text for argument in node.args for text in _direct_texts(argument)]
        return []
    if isinstance(node, ast.BinOp):
        # Аргументы подстановки не считаем постоянной частью шаблона.
        values = (
            [node.left] if isinstance(node.op, ast.Mod) else [node.left, node.right]
        )
        return [text for value in values for text in _direct_texts(value)]
    if isinstance(node, ast.IfExp):
        return _direct_texts(node.body) + _direct_texts(node.orelse)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [text for item in node.elts for text in _direct_texts(item)]
    return []


def _visible_values(call: ast.Call) -> list[ast.AST]:
    """Выбирает только известные поля вывода, не callback_data и не файлы."""
    name = (
        call.func.attr
        if isinstance(call.func, ast.Attribute)
        else getattr(call.func, "id", "")
    )
    is_method = name in TEXT_METHODS | TEXT_METHODS_WITH_CHAT
    is_send_method = name.startswith(("send_", "answer_", "reply_", "edit_"))
    values = []
    if name == "get_keyboard":
        values.extend(call.args)
    elif name in TEXT_METHODS or name == "LabeledPrice":
        values.extend(call.args[:1])
    elif name in TEXT_METHODS_WITH_CHAT:
        values.extend(call.args[1:2])
    if (
        is_method
        or is_send_method
        or name in UI_CONSTRUCTORS | {"button", "get_keyboard"}
    ):
        values.extend(kw.value for kw in call.keywords if kw.arg in VISIBLE_KEYWORDS)
    if name in KEYBOARD_FACTORIES:
        for kw in call.keywords:
            if kw.arg == "btns" and isinstance(kw.value, ast.Dict):
                values.extend(key for key in kw.value.keys if key is not None)
    return values


def _find_untranslated_ui(source: str, filename: str = "sample.py") -> list[str]:
    """Проверяет прямые выражения UI; произвольный поток данных AST не доказывает."""
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    errors = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parent = parents.get(node)
        while parent is not None and not isinstance(
            parent, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            parent = parents.get(parent)
        if parent is not None and (filename, parent.name) in SERVICE_FUNCTIONS:
            continue
        for value in _visible_values(node):
            for text in _direct_texts(value):
                errors.append(f"{filename}:{node.lineno}: {text!r}")
    return errors


def test_active_public_ui_has_no_direct_untranslated_human_labels() -> None:
    """Защищает активные публичные сценарии, не подключая ботов и БД."""
    sources = [
        ROOT / "app.py",
        *(ROOT / "handlers" / f"{name}.py" for name in ACTIVE_HANDLERS),
        *sorted((ROOT / "common").glob("*.py")),
        *sorted((ROOT / "filters").glob("*.py")),
        *sorted((ROOT / "middlewares").glob("*.py")),
    ]
    errors = []
    for path in sources:
        filename = path.relative_to(ROOT).as_posix()
        errors.extend(
            _find_untranslated_ui(path.read_text(encoding="utf-8-sig"), filename)
        )
    assert not errors, "Прямые строки UI вне gettext:\n" + "\n".join(errors)


@pytest.mark.parametrize(
    "source",
    [
        'InlineKeyboardButton(text="◀️ delete", callback_data="num_del")',
        'get_keyboard("Weather 🌊", "Words 🧩")',
        'message.answer("Сыграем в игру?")',
        'message.edit_text(f"Сумма: {amount}")',
        'bot.send_message(chat_id, "Пора сыграть")',
        'message.answer_photo(photo, caption="Новая игра")',
        'get_callback_btns(btns={"Удалить": "delete"})',
        'LabeledPrice(label="Поддержать автора", amount=1)',
        'message.answer("Игрок: {name}".format(name=message.from_user.full_name))',
        'message.answer("Готово" if result else "Ошибка")',
    ],
)
def test_guard_catches_untranslated_ui_mutants(source: str) -> None:
    """Проверяет, что защита ловит реальные варианты обхода переводов."""
    assert _find_untranslated_ui(source)


@pytest.mark.parametrize(
    "source",
    [
        'InlineKeyboardButton(text=_("Удалить"), callback_data="delete")',
        'get_keyboard(_("Игры 🕹"), _("Назад на главную ↩️"))',
        'message.answer(_("Игрок: {name}").format(name=message.from_user.full_name))',
        "message.answer(message.text)",
        'message.answer(data["user_name"])',
        'message.answer(f"{amount} XTR")',
        'message.answer(f"{amount} ⭐️")',
        'InlineKeyboardButton(text="5БУКВ", callback_data="Игра в разработке")',
        'InlineKeyboardButton(text="1", callback_data="num_1")',
        'LabeledPrice(label="XTR", amount=amount)',
        'message.answer_photo(FSInputFile(ROOT / "common/images/image_about.jpg"))',
        'logger.warning("Не удалось отправить сообщение")',
        'BotCommand(command="main", description="main panel")',
        "bot.get_chat_member(chat_id, user_id)",
        "bot.send_message(chat_id=home_group, text=service_text)",
    ],
)
def test_guard_ignores_translations_data_and_technical_values(source: str) -> None:
    """Не выдаёт ложных ошибок для данных, технических обозначений и логов."""
    assert not _find_untranslated_ui(source)


def test_guard_excludes_only_known_service_function_not_whole_file() -> None:
    """Служебное исключение не скрывает публичные сообщения того же модуля."""
    source = """
async def notify_service(bot):
    await bot.send_message(home_group, "Бот запущен")

async def public_handler(message):
    await message.answer("Игры")
"""
    errors = _find_untranslated_ui(source, "app.py")
    assert len(errors) == 1 and "Игры" in errors[0]
