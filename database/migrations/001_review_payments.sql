-- применять только после резервной копии и отдельного разрешения на выпуск
begin;

-- отказываемся работать с неожиданной базой или схемой
do $$
begin
    if current_database() not in ('bot_db', 'copilot_review_test') then
        raise exception 'unexpected database: migration stopped';
    end if;
    if to_regclass('public.users') is null or to_regclass('public.games') is null then
        raise exception 'expected application tables are missing';
    end if;
end;
$$;

-- сохраняем счета и подтверждения независимо от временного состояния диалога
create table payments (
    payment_id serial primary key,
    invoice_payload varchar(128) not null unique,
    user_id bigint not null,
    currency varchar(3) not null,
    total_amount integer not null,
    status varchar(20) not null,
    telegram_payment_charge_id varchar(255) unique,
    paid_at timestamp with time zone,
    refunded_at timestamp with time zone,
    created timestamp without time zone not null default now(),
    updated timestamp without time zone not null default now()
);

-- у пользователя telegram может не быть username
alter table games alter column user_name drop not null;
commit;
