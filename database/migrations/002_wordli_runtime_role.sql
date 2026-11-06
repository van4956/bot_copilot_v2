\set ON_ERROR_STOP on
\getenv wordli_password WORDLI_APP_PASSWORD

-- создаём отдельную роль только после явной миграции двух игровых таблиц
select exists(select 1 from pg_roles where rolname = 'copilot_wordli') as role_exists \gset
\if :role_exists
\echo 'Role copilot_wordli already exists; its password is not changed.'
\else
create role copilot_wordli login password :'wordli_password' nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
\endif

-- при ошибочных административных свойствах останавливаемся без исправлений
do $$
begin
    if exists(select 1 from pg_roles where rolname = 'copilot_wordli' and (rolsuper or rolcreatedb or rolcreaterole or rolreplication or rolbypassrls)) then
        raise exception 'copilot_wordli has administrative attributes';
    end if;
end
$$;

-- не меняем общие права public и не выдаём доступ к данным других функций
grant connect on database :"DBNAME" to copilot_wordli;
grant usage on schema public to copilot_wordli;
grant select, insert, update on table public.wordli_profiles, public.wordli_requests to copilot_wordli;
