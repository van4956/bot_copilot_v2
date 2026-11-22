# Выкладка КВОРДЛИ

Команды ниже — для согласованной выкладки из корня репозитория на VPS. КВОРДЛИ — отдельный Compose-проект `copilot-quordle`, единственный сервис `quordleapi`; порт `127.0.0.1:8795`. Стек не создаёт PostgreSQL, Redis и polling-бота. Сеть `bot_copilot_backend` уже должна существовать; вторая сеть нужна для Telegram. Ключи, профили, тестовое окружение и туннели не входят в образы.

### До запуска

1. Записать прежние теги или ID образов, сделать резервные копии изменяемых env/nginx и штатный резервный снимок PostgreSQL; хранить закрыто и отдельно от репозитория. Не выводить содержимое env в логи.
2. В **том же кластере PostgreSQL** выделить базу `copilot_quordle`, владельца схемы и отдельную runtime-роль `quordle_runtime` (не владелец, не администратор). От владельца целевой базы выполнить `psql -X -v ON_ERROR_STOP=1 -d copilot_quordle -f prototypes/quordle/migrations/001_initial.sql`. Миграция не выполняется при старте API.
3. Runtime-роли нужны только `CONNECT` к этой БД, `USAGE` схемы `public`, `SELECT, INSERT, UPDATE` на `public.quordle_players`. Не выдавать DDL, `DELETE`, `TRUNCATE`, членство в ролях с повышенными правами либо доступ к данным других игр. Учесть неявные права `PUBLIC`. Роль, пароль и право подключения настраивают вне репозитория; не использовать их в аргументах команд.
4. В защищённом `/etc/copilot/quordle.env` (права `0600`) задать:

   | Переменная | Назначение |
   |---|---|
   | `QUORDLE_DATABASE_URL` | `postgresql://quordle_runtime:<encoded-password>@db:5432/copilot_quordle`. Для спецсимволов в реквизитах нужен URL-encoding. |
   | `QUORDLE_PUBLIC_ORIGIN` | Постоянный `https://<игровой-хост>`, без пути. |
   | `QUORDLE_WEBAPP_URL` | Тот же origin + `/quordle/`, включая завершающий слэш. |
   | `BOT_TOKEN` | Токен единственного основного бота. |
   | `QUORDLE_EXPECTED_BOT_ID`, `QUORDLE_EXPECTED_BOT_USERNAME` | Известные идентификаторы **основного** бота; сверяются с `getMe`. |
   | `QUORDLE_REMINDERS_ENABLED` | `false` при проверке; `true` — для штатной отправки игрокам, которые её включили. |

   Compose сам задаёт `APP_ENV=production`; порт в env не переопределять. Для основного бота в его собственном env нужны только `QUORDLE_ENABLED` и тот же `QUORDLE_WEBAPP_URL`; настройки 5БУКВ не заменять.

### Порядок включения

```bash
export QUORDLE_ENV_FILE=/etc/copilot/quordle.env
export QUORDLE_IMAGE_TAG="<неизменяемый-тег-релиза>"
qcompose() { docker compose -p copilot-quordle -f docker/docker-compose.quordle.yml "$@"; }
qcompose config --quiet
qcompose build quordleapi
qcompose up -d --no-deps --no-build quordleapi
qcompose ps
curl -fsS http://127.0.0.1:8795/health
```

После ответа `{"ok": true}` добавить **только** содержимое `deployment/quordle/nginx-location.conf` (или include этого фрагмента) внутри действующего HTTPS server игр. Не заменять корневой `location /` 5БУКВ, сертификат и HTTP/ACME. Выполнить `nginx -t` до reload. Снаружи проверить `/quordle/health`, `/quordle/`, `/quordle/quordle.js`, `/quordle/quordle.css` и прежний `/api/health` 5БУКВ. POST без подписи Telegram должен быть отклонён; показ HTML без Telegram не подтверждает авторизацию игрока.

Только после проверки маршрута — задать основной кнопке `QUORDLE_ENABLED=true`, выполнить `docker compose --env-file .env -f docker/docker-compose.yml config --quiet`, собрать либо использовать уже проверенный образ бота с `COPILOT_IMAGE_TAG`, затем `docker compose --env-file .env -f docker/docker-compose.yml up -d --no-deps --no-build bot`. Проверить обе игры через «Слова» в основном Telegram-боте. Образы `bot_04_copilot_v2-bot`, `copilot-wordli`, `copilot-quordle` тегируются отдельно. Не применять общий `up`, `down` или `down -v`.

### Откат

Сначала убрать новую кнопку (`QUORDLE_ENABLED=false`), восстановить предыдущие подтверждённые теги и env, пересоздать **только** нужный сервис с `--no-deps --no-build`. При откате первого выпуска остановить `qcompose stop quordleapi`, вернуть только новое nginx-правило и проверить `nginx -t` перед reload. Профили, отдельную игровую базу, роли и тома не удалять; восстановление БД — отдельное решение по резервной копии. Проверить доступность основного бота и 5БУКВ.
