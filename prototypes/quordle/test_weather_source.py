"""Прогноз Open-Meteo для некоммерческого тестового бота, без ключей."""

import gettext
import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from common.http import ExternalServiceError, get_json
from common.locales import normalize_locale
from config_data.config import ROOT
from handlers import weather

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
FIELDS = (
    "temperature_2m", "relative_humidity_2m", "wind_speed_10m",
    "wind_direction_10m", "weather_code", "is_day",
)
NOT_FOUND = {
    "ru": "Населённый пункт не найден. Уточните название или отправьте GPS.",
    "en": "Place not found. Check the name or send a GPS location.",
    "uk": "Населений пункт не знайдено. Уточніть назву або надішліть GPS.",
    "es": "Lugar no encontrado. Comprueba el nombre o envía tu ubicación.",
}
ROUNDED = {
    "ru": "Округлено", "en": "Rounded", "uk": "Округлено", "es": "Redondeado",
}

# Группы WMO преобразуются в значки, уже принятые штатным форматтером.
ICON_GROUPS = {
    "01": (0,), "02": (1,), "03": (2,), "04": (3,), "50": (45, 48),
    "09": (51, 53, 55, 56, 57, 80, 81, 82), "10": (61, 63, 65, 66, 67),
    "13": (71, 73, 75, 77, 85, 86), "11": (95, 96, 97, 99),
}
ICONS = {code: group for group, codes in ICON_GROUPS.items() for code in codes}
WeatherRow = tuple[str, str, str, str, str]


def _coordinate(value: object, limit: int) -> bool:
    """Проверяет числовую координату до внешнего запроса."""
    return (
        isinstance(value, (float, int)) and not isinstance(value, bool)
        and math.isfinite(value) and abs(value) <= limit
    )


def _values(entry: dict) -> WeatherRow:
    """Готовит значения для штатного форматтера Copilot."""
    code = ICONS.get(entry["weather_code"], "")
    suffix = "d" if entry["is_day"] == 1 else "n"
    return weather._weather_values({
        "main": {
            "temp": entry["temperature_2m"],
            "humidity": entry["relative_humidity_2m"],
        },
        "wind": {
            "speed": entry["wind_speed_10m"],
            "deg": entry["wind_direction_10m"],
        },
        "weather": [{"icon": code + suffix}],
    })


def _period(data: dict, zone: ZoneInfo, header: str) -> str:
    """Показывает ближайшие трёхчасовые сроки в трёх местных датах."""
    hourly = data["hourly"]
    current = data["current"]["time"]
    dates = set()
    rows = []
    for index, stamp in enumerate(hourly["time"]):
        utc = datetime.fromtimestamp(stamp, timezone.utc)
        if stamp < current or utc.hour % 3:
            continue
        local = utc.astimezone(zone)
        dates.add(local.date())
        if len(dates) > 3:
            break
        entry = {key: hourly[key][index] for key in FIELDS}
        rows.append((local, _values(entry)))
    if len(dates) < 3:
        raise ValueError("Неполный трёхдневный прогноз")
    width = max(len(values[0]) for _, values in rows)
    lines = [header]
    previous = None
    for local, values in rows:
        if local.date() != previous:
            lines.append(f"\n{local:%d.%m}")
            previous = local.date()
        numeric = weather._numeric_row(values, width)
        lines.append(f"{local:%H:%M} | {numeric} {values[4]}")
    return "\n".join(lines)


async def fetch_test_weather(
    type_forecast: int,
    city: str | None,
    lat: float | None,
    lon: float | None,
    locale: str | None = "ru",
    *,
    api_key: str = "",
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Возвращает текущий или трёхдневный прогноз в штатном Telegram HTML."""
    language = normalize_locale(locale)
    translate = gettext.translation(
        "bot_00_template", ROOT / "locales", languages=[language],
        fallback=True,
    ).gettext
    name = "GPS"
    query = (city or "").strip()[:100]
    if type_forecast not in {1, 2} or (
        not query and not (_coordinate(lat, 90) and _coordinate(lon, 180))
    ):
        return translate("Неверные параметры запроса погоды")
    try:
        if query:
            matches = await get_json(
                GEOCODING, params={"name": query, "count": 1,
                                  "language": language}, session=session,
            )
            places = matches.get("results", [])
            if not places:
                return NOT_FOUND[language]
            place = places[0]
            lat, lon = place["latitude"], place["longitude"]
            if not (_coordinate(lat, 90) and _coordinate(lon, 180)):
                raise ValueError("Некорректный результат геокодирования")
            name = place["name"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Нет названия города")
        params: dict[str, Any] = {
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "timeformat": "unixtime", "wind_speed_unit": "ms",
            "temperature_unit": "celsius",
            "current": (
                ",".join(FIELDS) if type_forecast == 1 else "temperature_2m"
            ),
        }
        if type_forecast == 2:
            params.update(hourly=",".join(FIELDS), forecast_hours=76)
        data = await get_json(FORECAST, params=params, session=session)
        zone = ZoneInfo(data["timezone"])
        header = weather._weather_header(name, {"lat": lat, "lon": lon})
        if type_forecast == 1:
            current = data["current"]
            moment = datetime.fromtimestamp(current["time"], zone)
            values = _values(current)
            numeric = weather._numeric_row(values, current=True)
            body = (
                f"{header}\n\n{numeric} {values[4]}"
                f"\n\n{moment:%d-%m-%Y  %H:%M}"
            )
        else:
            body = _period(data, zone, header)
    except ExternalServiceError:
        return translate("Сервис погоды временно недоступен")
    except (AttributeError, IndexError, KeyError, OSError, OverflowError,
            TypeError, ValueError):
        return translate("Сервис погоды вернул некорректный ответ")
    sources = '<a href="https://open-meteo.com/">Open-Meteo</a>'
    if query:
        sources += ' · <a href="https://www.geonames.org/">GeoNames</a>'
    license_link = (
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>'
    )
    return f"{body}\n\n{sources} · {license_link} · {ROUNDED[language]}"
