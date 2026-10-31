"""Модели пользователей, рецептов, игр и платежей."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """первичный класс, от него дальше будут наследоваться все остальные"""

    created: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class Users(Base):
    """class Users соответствует таблице users в базе данных"""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    user_name: Mapped[str] = mapped_column(String(150), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    locale: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(150), nullable=False)
    flag: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # возможность для тротлинга


# class Cookbook соответствует таблице cookbook в базе данных
class Cookbook(Base):
    """class Cookbook соответствует таблице cookbook в базе данных"""

    __tablename__ = "cookbook"
    __table_args__ = {"sqlite_autoincrement": True}

    recipe_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recipe_name: Mapped[str] = mapped_column(String(150), nullable=False)
    author: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float(asdecimal=True), nullable=False)
    image: Mapped[str] = mapped_column(String(150))


# class Games соответствует таблице games в базе данных
class Games(Base):
    """class Games соответствует таблице games в базе данных"""

    __tablename__ = "games"

    game_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    game_name: Mapped[str] = mapped_column(String(150), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


class Payments(Base):
    """Выданные счета и уникальные подтверждения Telegram Stars."""

    __tablename__ = "payments"

    payment_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_payload: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refunded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
