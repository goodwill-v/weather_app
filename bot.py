import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import telebot
from telebot import types
from dotenv import load_dotenv

from weather_app import (
    get_current_weather,
    get_forecast_5d3h,
    get_coordinates,
    get_air_pollution,
    analyze_air_pollution,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не установлен BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)

DATA_PATH = Path(__file__).with_name("User_Data.json")
STATE_WAIT_CITY = "wait_city_weather"
STATE_WAIT_FORECAST_LOCATION = "wait_forecast_location"
STATE_WAIT_GEO_WEATHER = "wait_geo_weather"
STATE_WAIT_SUBSCRIBE = "wait_subscribe"
STATE_WAIT_COMPARE = "wait_compare"
STATE_WAIT_EXTENDED = "wait_extended"

user_states: dict[int, dict[str, Any]] = {}
forecast_cache: dict[int, dict[str, Any]] = {}


def _load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {"users": {}}
    try:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"users": {}}
        data.setdefault("users", {})
        if not isinstance(data["users"], dict):
            data["users"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"users": {}}


def _save_data(data: dict[str, Any]) -> None:
    try:
        with DATA_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _get_user_data(user_id: int) -> dict[str, Any]:
    data = _load_data()
    users = data.setdefault("users", {})
    user_data = users.setdefault(str(user_id), {})
    _save_data(data)
    return user_data


def _update_user_data(user_id: int, **updates: Any) -> dict[str, Any]:
    data = _load_data()
    users = data.setdefault("users", {})
    user_data = users.setdefault(str(user_id), {})
    user_data.update(updates)
    _save_data(data)
    return user_data


def _set_user_location(user_id: int, lat: float, lon: float) -> None:
    _update_user_data(user_id, location={"lat": lat, "lon": lon})


def _get_user_location(user_id: int) -> Optional[dict[str, float]]:
    data = _get_user_data(user_id)
    location = data.get("location")
    if isinstance(location, dict) and "lat" in location and "lon" in location:
        return {"lat": float(location["lat"]), "lon": float(location["lon"])}
    return None


def _set_user_last_city(user_id: int, city: str) -> None:
    _update_user_data(user_id, last_city=city)


def _get_user_last_city(user_id: int) -> Optional[str]:
    data = _get_user_data(user_id)
    value = data.get("last_city")
    return value if isinstance(value, str) else None


def _format_wind_direction(deg: Optional[int]) -> str:
    if deg is None:
        return "N/A"
    directions = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    index = round(deg / 45) % 8
    return directions[index]


def _format_sun_time(utc_timestamp: int, tz_offset_seconds: int) -> str:
    utc_dt = datetime.fromtimestamp(utc_timestamp, tz=timezone.utc)
    local_tz = timezone(timedelta(seconds=tz_offset_seconds))
    local_dt = utc_dt.astimezone(local_tz)
    return local_dt.strftime("%H:%M")


def _format_current_weather(weather: dict[str, Any]) -> str:
    main = weather.get("main", {})
    wind = weather.get("wind", {})
    sys_data = weather.get("sys", {})
    weather_info = weather.get("weather", [{}])[0]
    name = weather.get("name", "Неизвестно")
    description = weather_info.get("description", "N/A")

    wind_speed = wind.get("speed", "N/A")
    wind_gust = wind.get("gust")
    wind_deg = wind.get("deg")
    wind_info = f"{wind_speed} м/с"
    if wind_gust is not None:
        wind_info += f", порывы до {wind_gust} м/с"
    if wind_deg is not None:
        wind_info += f", {_format_wind_direction(wind_deg)} ({wind_deg}°)"

    tz_offset = weather.get("timezone", 0) or sys_data.get("timezone", 0)
    sunrise = sys_data.get("sunrise")
    sunset = sys_data.get("sunset")
    sun_line = ""
    if sunrise is not None and sunset is not None:
        sunrise_local = _format_sun_time(sunrise, tz_offset)
        sunset_local = _format_sun_time(sunset, tz_offset)
        sun_line = f"\nВосход: {sunrise_local} | Закат: {sunset_local}"

    return (
        f"Погода сейчас в {name}\n"
        f"Температура: {main.get('temp', 'N/A')}°C\n"
        f"Ощущается как: {main.get('feels_like', 'N/A')}°C\n"
        f"Описание: {description}\n"
        f"Влажность: {main.get('humidity', 'N/A')}%\n"
        f"Давление: {main.get('pressure', 'N/A')} hPa\n"
        f"Ветер: {wind_info}"
        f"{sun_line}"
    )


def _group_forecast_by_days(forecast_list: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in forecast_list:
        dt_txt = item.get("dt_txt", "")
        if dt_txt:
            date = dt_txt.split()[0]
            grouped.setdefault(date, []).append(item)
    return grouped


def _calculate_daily_average(day_forecasts: list[dict]) -> dict[str, Any]:
    if not day_forecasts:
        return {}
    temps = []
    humidities = []
    pressures = []
    wind_speeds = []
    descriptions = []
    for forecast in day_forecasts:
        main = forecast.get("main", {})
        weather_info = forecast.get("weather", [{}])[0]
        wind = forecast.get("wind", {})
        if "temp" in main:
            temps.append(main["temp"])
        if "humidity" in main:
            humidities.append(main["humidity"])
        if "pressure" in main:
            pressures.append(main["pressure"])
        if "speed" in wind:
            wind_speeds.append(wind["speed"])
        if "description" in weather_info:
            descriptions.append(weather_info["description"])

    def avg(values: list[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    description = max(set(descriptions), key=descriptions.count) if descriptions else "N/A"
    return {
        "temp_avg": avg(temps),
        "temp_min": min(temps) if temps else None,
        "temp_max": max(temps) if temps else None,
        "humidity_avg": avg(humidities),
        "pressure_avg": avg(pressures),
        "wind_speed_avg": avg(wind_speeds),
        "description": description,
    }


def _format_daily_summary(date: str, daily_data: dict[str, Any]) -> str:
    temp_avg = daily_data.get("temp_avg")
    temp_min = daily_data.get("temp_min")
    temp_max = daily_data.get("temp_max")
    description = daily_data.get("description", "N/A")
    humidity = daily_data.get("humidity_avg")
    wind = daily_data.get("wind_speed_avg")
    return (
        f"{date}\n"
        f"Температура: {temp_avg:.1f}°C (мин {temp_min:.1f}°C, макс {temp_max:.1f}°C)\n"
        f"Описание: {description}\n"
        f"Влажность: {humidity:.0f}%\n"
        f"Ветер: {wind:.1f} м/с"
    )


def _format_day_details(day_forecasts: list[dict]) -> str:
    lines = ["Детальный прогноз (шаг 3 часа):"]
    for forecast in day_forecasts:
        dt_txt = forecast.get("dt_txt", "N/A")
        main = forecast.get("main", {})
        weather_info = forecast.get("weather", [{}])[0]
        wind = forecast.get("wind", {})
        wind_speed = wind.get("speed", "N/A")
        wind_gust = wind.get("gust")
        wind_deg = wind.get("deg")
        wind_info = f"{wind_speed} м/с"
        if wind_gust is not None:
            wind_info += f", порывы до {wind_gust} м/с"
        if wind_deg is not None:
            wind_info += f", {_format_wind_direction(wind_deg)} ({wind_deg}°)"
        pop = forecast.get("pop")
        pop_line = f"\n  Вероятность осадков: {pop * 100:.0f}%" if pop is not None else ""
        lines.append(
            f"\n{dt_txt}\n"
            f"  Температура: {main.get('temp', 'N/A')}°C\n"
            f"  Ощущается как: {main.get('feels_like', 'N/A')}°C\n"
            f"  Описание: {weather_info.get('description', 'N/A')}\n"
            f"  Влажность: {main.get('humidity', 'N/A')}%\n"
            f"  Давление: {main.get('pressure', 'N/A')} hPa\n"
            f"  Ветер: {wind_info}"
            f"{pop_line}"
        )
    return "\n".join(lines)


def _build_main_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("Погода сейчас (город)", "Прогноз 5 дней (моя гео)")
    keyboard.row("Погода по гео", "Уведомления (вкл/выкл)")
    keyboard.row("Сравнение городов", "Расширенные данные")
    return keyboard


def _build_location_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    button = types.KeyboardButton("Отправить местоположение", request_location=True)
    keyboard.add(button)
    keyboard.add("Отмена")
    return keyboard


def _clear_state(user_id: int) -> None:
    user_states.pop(user_id, None)


def _send_forecast_inline(chat_id: int, user_id: int, lat: float, lon: float) -> None:
    forecast_list = get_forecast_5d3h(lat, lon)
    if not forecast_list:
        bot.send_message(chat_id, "Не удалось получить прогноз.")
        return
    grouped = _group_forecast_by_days(forecast_list)
    dates = sorted(grouped.keys())[:5]
    if not dates:
        bot.send_message(chat_id, "Нет данных прогноза.")
        return
    summaries = {date: _calculate_daily_average(grouped[date]) for date in dates}

    forecast_cache[user_id] = {
        "lat": lat,
        "lon": lon,
        "grouped": grouped,
        "dates": dates,
        "summaries": summaries,
    }

    lines = ["Прогноз на 5 дней:"]
    for date in dates:
        daily = summaries[date]
        temp_avg = daily.get("temp_avg")
        description = daily.get("description", "N/A")
        label = f"{temp_avg:.1f}°C" if temp_avg is not None else "N/A"
        lines.append(f"{date}: {label}, {description}")
    message_text = "\n".join(lines)

    keyboard = types.InlineKeyboardMarkup()
    for date in dates:
        daily = summaries[date]
        temp_avg = daily.get("temp_avg")
        short_date = date[5:] if len(date) >= 10 else date
        label = f"{short_date} {temp_avg:.0f}°C" if temp_avg is not None else short_date
        keyboard.add(types.InlineKeyboardButton(label, callback_data=f"fc_day|{date}"))

    previous_msg_id = user_states.get(user_id, {}).get("last_inline_msg_id")
    if previous_msg_id:
        try:
            bot.delete_message(chat_id, previous_msg_id)
        except Exception:
            pass

    sent = bot.send_message(chat_id, message_text, reply_markup=keyboard)
    user_states.setdefault(user_id, {})["last_inline_msg_id"] = sent.message_id


def _format_extended_weather(weather: dict[str, Any]) -> str:
    main = weather.get("main", {})
    wind = weather.get("wind", {})
    sys_data = weather.get("sys", {})
    coord = weather.get("coord", {})
    clouds = weather.get("clouds", {})
    weather_info = weather.get("weather", [{}])[0]
    name = weather.get("name", "Неизвестно")

    tz_offset = weather.get("timezone", 0) or sys_data.get("timezone", 0)
    sunrise = sys_data.get("sunrise")
    sunset = sys_data.get("sunset")
    sunrise_local = _format_sun_time(sunrise, tz_offset) if sunrise else "N/A"
    sunset_local = _format_sun_time(sunset, tz_offset) if sunset else "N/A"

    wind_speed = wind.get("speed", "N/A")
    wind_gust = wind.get("gust")
    wind_deg = wind.get("deg")
    wind_info = f"{wind_speed} м/с"
    if wind_gust is not None:
        wind_info += f", порывы до {wind_gust} м/с"
    if wind_deg is not None:
        wind_info += f", {_format_wind_direction(wind_deg)} ({wind_deg}°)"

    pollution_line = "Загрязнение воздуха: нет данных"
    lat = coord.get("lat")
    lon = coord.get("lon")
    if lat is not None and lon is not None:
        pollution = get_air_pollution(lat, lon)
        analysis = analyze_air_pollution(pollution, extended=True) if pollution else {}
        if analysis:
            aqi = analysis.get("aqi", "N/A")
            level = analysis.get("level_name", "N/A")
            pollution_line = f"Загрязнение воздуха: AQI {aqi} — {level}"

    return (
        f"Расширенные данные: {name}\n"
        f"Температура: {main.get('temp', 'N/A')}°C\n"
        f"Ощущается как: {main.get('feels_like', 'N/A')}°C\n"
        f"Описание: {weather_info.get('description', 'N/A')}\n"
        f"Влажность: {main.get('humidity', 'N/A')}%\n"
        f"Давление: {main.get('pressure', 'N/A')} hPa\n"
        f"Облачность: {clouds.get('all', 'N/A')}%\n"
        f"Видимость: {weather.get('visibility', 'N/A')} м\n"
        f"Ветер: {wind_info}\n"
        f"Восход: {sunrise_local} | Закат: {sunset_local}\n"
        f"УФ индекс: нет данных\n"
        f"{pollution_line}"
    )


def _set_subscription(user_id: int, enabled: bool) -> None:
    _update_user_data(
        user_id,
        subscription={
            "enabled": enabled,
            "last_rain_alert": None,
            "last_condition": None,
            "last_condition_time": None,
        },
    )


def _get_subscription(user_id: int) -> dict[str, Any]:
    data = _get_user_data(user_id)
    value = data.get("subscription")
    if isinstance(value, dict):
        return value
    return {"enabled": False}


def _check_tomorrow_rain(forecast_list: list[dict]) -> bool:
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    for item in forecast_list:
        dt_txt = item.get("dt_txt")
        if not dt_txt:
            continue
        try:
            dt = datetime.fromisoformat(dt_txt)
        except ValueError:
            continue
        if dt.date() == tomorrow:
            weather_info = item.get("weather", [{}])[0]
            description = str(weather_info.get("description", "")).lower()
            pop = item.get("pop", 0)
            if "дожд" in description or pop >= 0.4:
                return True
    return False


def _notification_loop() -> None:
    while True:
        time.sleep(2 * 60 * 60)
        data = _load_data()
        users = data.get("users", {})
        if not isinstance(users, dict):
            continue
        for user_id_str, user_data in users.items():
            if not isinstance(user_data, dict):
                continue
            subscription = user_data.get("subscription", {})
            if not isinstance(subscription, dict) or not subscription.get("enabled"):
                continue
            location = user_data.get("location")
            if not isinstance(location, dict):
                continue
            lat = location.get("lat")
            lon = location.get("lon")
            if lat is None or lon is None:
                continue
            user_id = int(user_id_str)
            try:
                forecast_list = get_forecast_5d3h(lat, lon)
                current_weather = get_current_weather(latitude=lat, longitude=lon)
            except Exception:
                continue
            if not forecast_list or not current_weather:
                continue

            now_date = datetime.now().date().isoformat()
            if _check_tomorrow_rain(forecast_list):
                last_alert = subscription.get("last_rain_alert")
                if last_alert != now_date:
                    bot.send_message(
                        user_id,
                        "Предупреждение: завтра возможен дождь. Возьмите зонт.",
                    )
                    subscription["last_rain_alert"] = now_date

            current_desc = ""
            weather_info = current_weather.get("weather", [{}])[0]
            if isinstance(weather_info, dict):
                current_desc = str(weather_info.get("description", ""))

            last_condition = subscription.get("last_condition")
            if current_desc and current_desc != last_condition:
                bot.send_message(
                    user_id,
                    f"Изменение погоды: сейчас {current_desc.lower()}.",
                )
                subscription["last_condition"] = current_desc
                subscription["last_condition_time"] = datetime.now().isoformat()

            user_data["subscription"] = subscription
            users[user_id_str] = user_data
        data["users"] = users
        _save_data(data)
@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "Привет! Я погодный бот. Выберите функцию в меню ниже.",
        reply_markup=_build_main_keyboard(),
    )


@bot.message_handler(content_types=["location"])
def handle_location(message: types.Message) -> None:
    user_id = message.from_user.id
    lat = message.location.latitude
    lon = message.location.longitude
    _set_user_location(user_id, lat, lon)

    state = user_states.get(user_id, {}).get("state")
    _clear_state(user_id)

    if state == STATE_WAIT_FORECAST_LOCATION:
        _send_forecast_inline(message.chat.id, user_id, lat, lon)
        bot.send_message(message.chat.id, "Геопозиция сохранена.", reply_markup=_build_main_keyboard())
        return
    if state == STATE_WAIT_GEO_WEATHER:
        weather = get_current_weather(latitude=lat, longitude=lon)
        if isinstance(weather, dict):
            bot.send_message(message.chat.id, _format_current_weather(weather))
        else:
            bot.send_message(message.chat.id, "Не удалось получить погоду.")
        return
    if state == STATE_WAIT_EXTENDED:
        weather = get_current_weather(latitude=lat, longitude=lon)
        if isinstance(weather, dict):
            bot.send_message(message.chat.id, _format_extended_weather(weather))
        else:
            bot.send_message(message.chat.id, "Не удалось получить данные.")
        return

    weather = get_current_weather(latitude=lat, longitude=lon)
    if isinstance(weather, dict):
        bot.send_message(message.chat.id, _format_current_weather(weather))
    else:
        bot.send_message(message.chat.id, "Не удалось получить погоду.")


@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message) -> None:
    user_id = message.from_user.id
    text = message.text.strip()

    if text == "Отмена":
        _clear_state(user_id)
        bot.send_message(message.chat.id, "Отменено.", reply_markup=_build_main_keyboard())
        return

    state = user_states.get(user_id, {}).get("state")

    if state == STATE_WAIT_CITY:
        _clear_state(user_id)
        city = text
        _set_user_last_city(user_id, city)
        weather = get_current_weather(city=city)
        if isinstance(weather, dict):
            bot.send_message(message.chat.id, _format_current_weather(weather))
        else:
            bot.send_message(message.chat.id, "Не удалось получить погоду.")
        return

    if state == STATE_WAIT_COMPARE:
        _clear_state(user_id)
        parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
        if len(parts) != 2:
            bot.send_message(
                message.chat.id,
                "Введите два города через запятую. Пример: Москва, Казань",
            )
            return
        city_a, city_b = parts
        weather_a = get_current_weather(city=city_a)
        weather_b = get_current_weather(city=city_b)
        if not isinstance(weather_a, dict) or not isinstance(weather_b, dict):
            bot.send_message(message.chat.id, "Не удалось получить данные для сравнения.")
            return
        rows = [
            ("Город", "Темп", "Ощущ.", "Влажн."),
            (
                city_a,
                f"{weather_a.get('main', {}).get('temp', 'N/A')}°C",
                f"{weather_a.get('main', {}).get('feels_like', 'N/A')}°C",
                f"{weather_a.get('main', {}).get('humidity', 'N/A')}%",
            ),
            (
                city_b,
                f"{weather_b.get('main', {}).get('temp', 'N/A')}°C",
                f"{weather_b.get('main', {}).get('feels_like', 'N/A')}°C",
                f"{weather_b.get('main', {}).get('humidity', 'N/A')}%",
            ),
        ]
        col_widths = [max(len(str(row[i])) for row in rows) for i in range(4)]
        lines = []
        for row in rows:
            line = " | ".join(str(row[i]).ljust(col_widths[i]) for i in range(4))
            lines.append(line)
        table = "```\n" + "\n".join(lines) + "\n```"
        bot.send_message(message.chat.id, table, parse_mode="Markdown")
        return

    if state == STATE_WAIT_SUBSCRIBE:
        _clear_state(user_id)
        answer = text.lower()
        enabled = answer in ("да", "yes", "y", "д")
        _set_subscription(user_id, enabled)
        status = "включены" if enabled else "отключены"
        bot.send_message(message.chat.id, f"Погодные уведомления {status}.")
        return

    if state == STATE_WAIT_EXTENDED:
        _clear_state(user_id)
        city = text
        _set_user_last_city(user_id, city)
        weather = get_current_weather(city=city)
        if isinstance(weather, dict):
            bot.send_message(message.chat.id, _format_extended_weather(weather))
        else:
            bot.send_message(message.chat.id, "Не удалось получить данные.")
        return

    if text == "Погода сейчас (город)":
        user_states[user_id] = {"state": STATE_WAIT_CITY}
        last_city = _get_user_last_city(user_id)
        hint = f" (например, {last_city})" if last_city else ""
        bot.send_message(message.chat.id, f"Введите название города{hint}.")
        return

    if text == "Прогноз 5 дней (моя гео)":
        location = _get_user_location(user_id)
        if location:
            _send_forecast_inline(message.chat.id, user_id, location["lat"], location["lon"])
        else:
            user_states[user_id] = {"state": STATE_WAIT_FORECAST_LOCATION}
            bot.send_message(
                message.chat.id,
                "Отправьте местоположение для прогноза на 5 дней.",
                reply_markup=_build_location_keyboard(),
            )
        return

    if text == "Погода по гео":
        user_states[user_id] = {"state": STATE_WAIT_GEO_WEATHER}
        bot.send_message(
            message.chat.id,
            "Отправьте местоположение.",
            reply_markup=_build_location_keyboard(),
        )
        return

    if text == "Уведомления (вкл/выкл)":
        user_states[user_id] = {"state": STATE_WAIT_SUBSCRIBE}
        bot.send_message(message.chat.id, "Включить уведомления? (да/нет)")
        return

    if text == "Сравнение городов":
        user_states[user_id] = {"state": STATE_WAIT_COMPARE}
        bot.send_message(
            message.chat.id,
            "Введите два города через запятую. Пример: Москва, Казань",
        )
        return

    if text == "Расширенные данные":
        user_states[user_id] = {"state": STATE_WAIT_EXTENDED}
        bot.send_message(
            message.chat.id,
            "Введите город или отправьте местоположение.",
            reply_markup=_build_location_keyboard(),
        )
        return

    if text.startswith("/"):
        bot.send_message(message.chat.id, "Команда не распознана. Используйте меню.")
        return

    bot.send_message(
        message.chat.id,
        "Выберите действие в меню ниже.",
        reply_markup=_build_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("fc_"))
def handle_forecast_callback(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    payload = call.data
    cache = forecast_cache.get(user_id)
    if not cache:
        bot.answer_callback_query(call.id, "Нет данных прогноза. Запросите снова.")
        return

    if payload == "fc_back":
        dates = cache.get("dates", [])
        summaries = cache.get("summaries", {})
        lines = ["Прогноз на 5 дней:"]
        for date in dates:
            daily = summaries.get(date, {})
            temp_avg = daily.get("temp_avg")
            description = daily.get("description", "N/A")
            label = f"{temp_avg:.1f}°C" if temp_avg is not None else "N/A"
            lines.append(f"{date}: {label}, {description}")
        message_text = "\n".join(lines)

        keyboard = types.InlineKeyboardMarkup()
        for date in dates:
            daily = summaries.get(date, {})
            temp_avg = daily.get("temp_avg")
            short_date = date[5:] if len(date) >= 10 else date
            label = f"{short_date} {temp_avg:.0f}°C" if temp_avg is not None else short_date
            keyboard.add(types.InlineKeyboardButton(label, callback_data=f"fc_day|{date}"))

        bot.edit_message_text(
            message_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=keyboard,
        )
        bot.answer_callback_query(call.id)
        return

    if payload.startswith("fc_day|"):
        date = payload.split("|", 1)[1]
        grouped = cache.get("grouped", {})
        day_forecasts = grouped.get(date)
        if not day_forecasts:
            bot.answer_callback_query(call.id, "Нет данных по дню.")
            return
        details = _format_day_details(day_forecasts)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Назад", callback_data="fc_back"))
        bot.edit_message_text(
            details,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=keyboard,
        )
        bot.answer_callback_query(call.id)


def _start_notification_thread() -> None:
    thread = threading.Thread(target=_notification_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    _start_notification_thread()
    bot.infinity_polling()