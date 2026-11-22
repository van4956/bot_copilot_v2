-- миграцию запускает владелец новой выделенной базы, не игровая роль
begin;

do $guard$
begin
    if current_database() <> 'copilot_quordle' then
        raise exception 'Миграция предназначена только для базы copilot_quordle';
    end if;
end;
$guard$;

-- личная история, статистика и настройки не зависят от других игр
create table if not exists public.quordle_players (
    user_id bigint primary key check (user_id > 0),
    display_name text not null,
    locale text not null default 'ru',
    game_data jsonb not null default '{}',
    revision bigint not null default 0,
    rating integer,
    played integer not null default 0,
    reminders_enabled boolean not null default false,
    timezone_name text not null default 'Europe/Moscow',
    reminded_on date
);

-- в рейтинг попадают только игроки с рассчитанными очками
create index if not exists quordle_rating_idx
    on public.quordle_players (rating desc, played desc) where rating is not null;

commit;
