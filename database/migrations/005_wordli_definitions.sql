\set ON_ERROR_STOP on

begin;

-- общий кэш не содержит идентификаторов игроков и не связан с их статистикой
create table if not exists public.wordli_definitions (
    word varchar(5) primary key,
    data json not null,
    checked_at bigint not null,
    retry_after bigint not null
);

-- процесс игры читает и обновляет кэш без ddl и права удаления
grant select, insert, update on table public.wordli_definitions to copilot_wordli;

commit;
