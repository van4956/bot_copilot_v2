"""Команды личного меню пользователя и администратора."""

from aiogram.types import BotCommand

private_command = [
    BotCommand(command="main", description="main pannel"),
    BotCommand(command="book", description="cookbook"),
    # Мини-приложения временно скрыты, код раздела сохранён.
    # BotCommand(command="mini", description="miniapps"),
    BotCommand(command="info", description="settings"),
]

admin_command = [
    BotCommand(command="main", description="main pannel"),
    BotCommand(command="book", description="cookbook"),
    # BotCommand(command="mini", description="miniapps"),
    BotCommand(command="info", description="settings"),
]
