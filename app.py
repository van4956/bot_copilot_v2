import logging

# Настраиваем базовую конфигурацию логирования
# WARNING - самое важное, для прода, для контейнера
# INFO - подробный, для отладки
# logging.basicConfig(level=logging.WARNING, format=' - [%(asctime)s] #%(levelname)-5s -  %(name)s:%(lineno)d  -  %(message)s')
logging.basicConfig(level=logging.WARNING, format=' - [%(asctime)s] #%(levelname)-5s - %(name)s:%(lineno)d  -  %(message)s', datefmt='%y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

# Настраиваем логгер для SQLAlchemy
sqlalchemy_logger = logging.getLogger('sqlalchemy.engine')
sqlalchemy_logger.setLevel(logging.INFO)
sqlalchemy_logger.propagate = True  # Отключаем передачу сообщений основному логгеру, чтобы не задваивать их

from icecream import ic
ic.configureOutput(includeContext=True, prefix=' >>> Debug >>> ')

import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types
from aiogram.types import Update
from aiogram.fsm.strategy import FSMStrategy
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.i18n import ConstI18nMiddleware, I18n, SimpleI18nMiddleware, FSMI18nMiddleware
from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config_data.config import Config, load_config

from handlers import other, admin, group, start, owner, donate, private, weather, currency, llm, cookbook, miniapp
from common.comands import private_command, admin_command
from database.models import Base
from middlewares import counter, db, locale, throttle


# Режим запуска:
# docker == 1 - запуск в docker (docker-compose up --build)
# docker == 0 - запуск локально (python app.py)
docker = 1

# Загружаем конфиг в переменную config
config: Config = load_config()

# Инициализируем объект хранилища
if docker == 1:
    # Для Docker режима данные хранятся на отдельном сервере Redis
    storage = RedisStorage(
        redis=Redis(
            host=config.redis.host,
            port=config.redis.port))
else:
    # Иначе, данные хранятся в оперативной памяти, при перезапуске всё стирается (для тестов и разработки)
    storage = MemoryStorage()

# Формируем рабочий токен бота если в Docker режиме, иначе используем тестовый токен
if docker == 1:
    token = config.tg_bot.token
else:
    token = config.tg_bot.token_test

logger.info('Инициализируем бот и диспетчер')
bot = Bot(token=token,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML, # для html тегов в сообщениях
                                       link_preview=None, # отключаем превью ссылок
                                       link_preview_is_disabled=None, # отключаем превью ссылок
                                       link_preview_prefer_large_media=None, # отключаем превью ссылок
                                       link_preview_prefer_small_media=None, # отключаем превью ссылок
                                       link_preview_show_above_text=None)) # отключаем превью ссылок
bot.owner = config.tg_bot.owner
bot.admin_list = config.tg_bot.admin_list
bot.home_group = config.tg_bot.home_group
bot.work_group = config.tg_bot.work_group
bot.api_gpt = config.tg_bot.api_gpt
bot.api_weather = config.tg_bot.api_weather
bot.api_currency = config.tg_bot.api_currency


dp = Dispatcher(fsm_strategy=FSMStrategy.USER_IN_CHAT, storage=storage)
# USER_IN_CHAT  -  для каждого юзера, в каждом чате ведется своя запись состояний (это по дефолту)
# GLOBAL_USER  -  для каждого юзера везде ведется своё состояние

# Создаем движок бд
if docker == 1: # PostgreSQL
    engine = create_async_engine(config.db.db_post, echo=False)
else: # SQLite (для тестов и разработки)
    engine = create_async_engine(config.db.db_lite, echo=False)

# Создаем ассинхроную сессию
session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

# Помещаем нужные объекты в workflow_data диспетчера
some_var_1 = 1
some_var_2 = 'Some text'
dp.workflow_data.update({'my_int_var': some_var_1,
                         'my_text_var': some_var_2})

# Подключаем мидлвари
dp.update.outer_middleware(throttle.ThrottleMiddleware())  # тротлинг чрезмерно частых действий пользователей
dp.update.outer_middleware(counter.CounterMiddleware())  # простой счетчик
dp.update.outer_middleware(db.DataBaseSession(session_pool=session_maker))  # мидлварь для прокидывания сессии БД
dp.update.outer_middleware(locale.LocaleFromDBMiddleware(workflow_data=dp.workflow_data))  # определяем локаль из БД и передам ее в FSMContext
i18n = I18n(path="locales", default_locale="ru", domain="bot_00_template")  # создаем объект I18n
dp.update.middleware(FSMI18nMiddleware(i18n=i18n))  # получяем язык на каждый апдейт, через обращение к FSMContext

# dp.update.middleware(ConstI18nMiddleware(locale='ru', i18n=i18n))  # задаем локаль как принудительно устанавливаемую константу
# dp.update.middleware(SimpleI18nMiddleware(i18n=i18n))  # задаем локаль по значению поля "language_code" апдейта

# Подключаем роутеры
dp.include_router(start.start_router)
dp.include_router(owner.owner_router)
dp.include_router(admin.admin_router)
dp.include_router(private.private_router)
dp.include_router(weather.weather_router)
dp.include_router(currency.currency_router)
dp.include_router(cookbook.cookbook_router)
# dp.include_router(llm.llm_router)
dp.include_router(miniapp.miniapp_router)
dp.include_router(donate.donate_router)
dp.include_router(group.group_router)
dp.include_router(other.other_router)

# Логируем все необработанные апдейты
@dp.update()
async def log_all_updates(update: Update):
    logger.info("Необработанный апдейт: %s", update)

# Типы апдейтов которые будем отлавливать ботом
# ALLOWED_UPDATES = ['message',
#                     'edited_message',
#                     'callback_query',
#                     "web_app_data",
#                     "chat_member",
#                     "pre_checkout_query",
#                     "successful_payment"]  # Отбираем определенные типы апдейтов
ALLOWED_UPDATES = dp.resolve_used_update_types()  # Отбираем только используемые события по роутерам

# Функция сработает при запуске бота
async def on_startup():
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    await bot.send_message(chat_id = bot.home_group[0], text = f"🤖 @{bot_username}  -  запущен!")

# Функция сработает при остановке работы бота
async def on_shutdown():
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    await bot.send_message(chat_id = bot.home_group[0], text = f"☠️ @{bot_username}  -  деактивирован!")



# Главная функция конфигурирования и запуска бота
async def main() -> None:

    # Удаление предыдущей версии базы, и создание новых таблиц заново
    async with engine.begin() as connection:
        # await connection.run_sync(Base.metadata.drop_all) # удаляем все таблицы
        await connection.run_sync(Base.metadata.create_all) # создаем все таблицы

    # Регистрируем функцию, которая будет вызвана автоматически при запуске/остановке бота
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Пропускаем накопившиеся апдейты - удаляем вебхуки (то что бот получил пока спал)
    await bot.delete_webhook(drop_pending_updates=True)

    # Удаляем ранее установленные команды для бота во всех личных чатах
    await bot.delete_my_commands(scope=types.BotCommandScopeAllPrivateChats())

    # Добавляем свои команды
    await bot.set_my_commands(commands=private_command, scope=types.BotCommandScopeAllPrivateChats())

    # Добавляем команды для админов
    for admin_id in bot.admin_list:
        await bot.set_my_commands(commands=admin_command, scope=types.BotCommandScopeChat(chat_id=admin_id))

    # Запускаем polling
    try:
        await dp.start_polling(bot,
                               allowed_updates=ALLOWED_UPDATES,
                               polling_timeout=60)
                            #    skip_updates=False)  # Если бот будет обрабатывать платежи, НЕ пропускаем обновления!
    finally:
        await bot.session.close()



if __name__ == "__main__":
    asyncio.run(main())
