\set ON_ERROR_STOP on

-- перед будущей выкладкой разрешает только чтение языка по telegram id
-- не даёт доступа к именам, статусам, платежам или изменению пользователей
begin;
grant select (user_id, locale) on table public.users to copilot_wordli;
commit;
