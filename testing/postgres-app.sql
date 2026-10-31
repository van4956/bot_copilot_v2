-- создаём непривилегированного пользователя только для тестовой базы
create role bot_app login password :'app_password' nosuperuser nocreatedb nocreaterole noreplication;
grant connect on database copilot_review_test to bot_app;
-- разрешаем изолированные схемы для проверки миграций только в тестовой базе
grant create on database copilot_review_test to bot_app;
grant usage, create on schema public to bot_app;
