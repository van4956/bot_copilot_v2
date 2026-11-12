-- создаёт только английские тексты; русские рецепты не изменяет
begin;
create table if not exists cookbook_en (
    recipe_id integer primary key references cookbook(recipe_id) on delete cascade,
    recipe_name varchar(300) not null,
    description text not null,
    source_hash varchar(64) not null,
    model varchar(100) not null,
    created timestamp without time zone not null default current_timestamp,
    updated timestamp without time zone not null default current_timestamp
);
commit;
-- права рабочей роли выдаются владельцем схемы отдельно перед будущей выкладкой
