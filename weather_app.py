import requests
from dotenv import load_dotenv
import os
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
load_dotenv()
API_KEY = os.getenv("API_KEY")

CACHE_PATH = Path(__file__).with_name("weather_cache.json")
MAX_CACHE_AGE_SECONDS = 3 * 60 * 60  # 3 hours


class TransientRequestError(RuntimeError):
    """Raised when a request failed after retries due to transient conditions."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_fresh(fetched_at_iso: str, max_age_seconds: int = MAX_CACHE_AGE_SECONDS) -> bool:
    dt = _parse_iso_datetime(fetched_at_iso)
    if not dt:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return 0 <= age <= max_age_seconds


def _load_cache() -> Optional[dict[str, Any]]:
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(*, city: Optional[str], lat: float, lon: float, weather_data: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "fetched_at": _now_utc_iso(),
        "data": weather_data,
    }
    try:
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        # Cache failures shouldn't break the app
        pass


def _floats_close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _cache_matches_request(
    cache: dict[str, Any],
    *,
    city: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
) -> bool:
    cache_city = cache.get("city")
    cache_lat = cache.get("lat")
    cache_lon = cache.get("lon")

    if city is not None and isinstance(cache_city, str):
        return cache_city.strip().casefold() == city.strip().casefold()

    if lat is not None and lon is not None and isinstance(cache_lat, (int, float)) and isinstance(cache_lon, (int, float)):
        return _floats_close(float(cache_lat), float(lat)) and _floats_close(float(cache_lon), float(lon))

    return False


def _get_error_message(status_code: int) -> str:
    """Возвращает короткое сообщение об ошибке по HTTP статус-коду."""
    error_messages = {
        401: "Неверный API ключ",
        404: "Город или координаты не найдены",
        429: "Превышен лимит запросов",
    }
    
    if status_code in error_messages:
        return error_messages[status_code]
    
    if 500 <= status_code <= 599:
        return "Ошибка сервера"
    
    return f"Ошибка HTTP {status_code}"


def _request_with_retries(url: str, *, timeout_seconds: float = 10.0) -> requests.Response:
    backoffs = [1, 2, 4]
    last_exc: Optional[BaseException] = None
    last_resp: Optional[requests.Response] = None

    for attempt in range(len(backoffs) + 1):
        try:
            resp = requests.get(url, timeout=timeout_seconds)
            last_resp = resp

            # Retry on throttling or server-side temporary errors
            if resp.status_code == 429 or 500 <= resp.status_code <= 599:
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
                    continue
                raise TransientRequestError(f"HTTP {resp.status_code} after retries")

            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < len(backoffs):
                time.sleep(backoffs[attempt])
                continue
            raise TransientRequestError("Network error after retries") from e

    # Unreachable, but keeps type-checkers happy
    if last_exc:
        raise TransientRequestError("Network error") from last_exc
    if last_resp is not None:
        raise TransientRequestError(f"HTTP {last_resp.status_code}")
    raise TransientRequestError("Unknown request error")


def get_current_weather(city: str=None, latitude: float=None, longitude: float=None) -> dict:
    if city:
        print(f"Получаем погоду для города: {city}")
        coords = get_coordinates(city)
        if not coords:
            return None
        latitude, longitude = coords
        weather = get_weather_by_coordinates(latitude, longitude)
        if isinstance(weather, dict):
            _save_cache(city=city, lat=latitude, lon=longitude, weather_data=weather)
        return weather
    if latitude is not None and longitude is not None:
        print(f"Получаем погоду для координат: {latitude}, {longitude}")
        weather = get_weather_by_coordinates(latitude, longitude)
        if isinstance(weather, dict):
            _save_cache(city=None, lat=latitude, lon=longitude, weather_data=weather)
        return weather

def get_coordinates(city: str) -> tuple:
    url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&appid={API_KEY}"
    response = _request_with_retries(url)
    if response.status_code == 200:
        data = response.json()
        if not data:
            print("Ошибка: геокодер вернул пустой список координат")
            return None
        return data[0]["lat"], data[0]["lon"]
    else:
        print(_get_error_message(response.status_code))
        return None

def get_weather_by_coordinates(latitude: float, longitude: float) -> dict:
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={API_KEY}&units=metric&lang=ru"
    response = _request_with_retries(url)
    if response.status_code == 200:
        return response.json()
    else:
        print(_get_error_message(response.status_code))
        return None


def get_forecast_5d3h(lat: float, lon: float) -> list[dict]:
    """Получает 5-дневный прогноз погоды с шагом 3 часа."""
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang=ru"
    response = _request_with_retries(url)
    if response.status_code == 200:
        data = response.json()
        return data.get('list', [])
    else:
        print(_get_error_message(response.status_code))
        return []


def get_air_pollution(lat: float, lon: float) -> dict:
    """
    Получает данные о загрязнении воздуха.
    
    Returns:
        dict: Словарь с данными из list[0], включая components и main (AQI)
    """
    url = f"http://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={API_KEY}"
    response = _request_with_retries(url)
    if response.status_code == 200:
        data = response.json()
        # Возвращаем первый элемент из списка (текущие данные)
        # Содержит: dt, main (с aqi), components
        if data.get('list') and len(data['list']) > 0:
            return data['list'][0]
        return {}
    else:
        print(_get_error_message(response.status_code))
        return {}


def analyze_air_pollution(components: dict, extended: bool = False) -> dict:
    """
    Анализирует данные о загрязнении воздуха и возвращает сводный статус.
    
    Args:
        components: Словарь с компонентами загрязнения из API
        extended: Если True, возвращает детальную информацию
    
    Returns:
        Словарь с анализом загрязнения воздуха
    """
    if not components:
        return {"status": "Нет данных", "aqi": None}
    
    # Получаем AQI из main (если передан полный объект) или вычисляем
    aqi = None
    if isinstance(components, dict) and 'main' in components:
        aqi = components['main'].get('aqi')
        comps = components.get('components', {})
    else:
        comps = components
    
    # Нормативные значения (µg/m³) для уровня "Fair" (2) - допустимые
    # Превышение этих значений считается плохим (уровень 4+)
    thresholds = {
        'co': 9400,      # CO в µg/m³ (Fair: 4400-9400, Poor: 12400-15400)
        'no': None,      # NO обычно не используется в AQI
        'no2': 150,      # NO2 (Fair: 40-70, Moderate: 70-150, Poor: 150-200)
        'o3': 140,       # O3 (Fair: 60-100, Moderate: 100-140, Poor: 140-180)
        'so2': 250,      # SO2 (Fair: 20-80, Moderate: 80-250, Poor: 250-350)
        'pm2_5': 50,     # PM2.5 (Fair: 10-25, Moderate: 25-50, Poor: 50-75)
        'pm10': 100,     # PM10 (Fair: 20-50, Moderate: 50-100, Poor: 100-200)
        'nh3': None,     # NH3 обычно не используется в AQI
    }
    
    # Определяем уровень AQI на русском
    aqi_levels = {
        1: "Хорошо",
        2: "Удовлетворительно",
        3: "Умеренно",
        4: "Плохо",
        5: "Очень плохо"
    }
    
    # Если AQI не передан, пытаемся определить по компонентам
    if aqi is None:
        # Упрощенная оценка на основе компонентов
        max_level = 1
        for key, value in comps.items():
            if value is None:
                continue
            key_lower = key.lower()
            threshold = thresholds.get(key_lower)
            if threshold and value > threshold:
                if value > threshold * 1.5:  # Очень плохо
                    max_level = max(max_level, 5)
                elif value > threshold:  # Плохо
                    max_level = max(max_level, 4)
        aqi = max_level
    
    status = aqi_levels.get(aqi, "Неизвестно")
    
    result = {
        "status": status,
        "aqi": aqi,
        "level_name": aqi_levels.get(aqi, "Неизвестно")
    }
    
    # При уровне 4 (Плохо) или выше определяем превышающие параметры
    if aqi >= 4:
        exceeded = []
        for key, value in comps.items():
            if value is None:
                continue
            key_lower = key.lower()
            threshold = thresholds.get(key_lower)
            if threshold and value > threshold:
                exceeded.append({
                    "component": key,
                    "value": value,
                    "threshold": threshold,
                    "excess": round(value - threshold, 2)
                })
        result["exceeded_parameters"] = exceeded
    
    # Детальная информация при extended=True
    if extended:
        result["components"] = comps
        result["thresholds"] = thresholds
    
    return result


def _group_forecast_by_days(forecast_list: list[dict]) -> dict[str, list[dict]]:
    """Группирует прогноз по дням."""
    from collections import defaultdict
    grouped = defaultdict(list)
    
    for item in forecast_list:
        dt_txt = item.get('dt_txt', '')
        if dt_txt:
            # Извлекаем дату (YYYY-MM-DD)
            date = dt_txt.split()[0]
            grouped[date].append(item)
    
    return dict(grouped)


def _calculate_daily_average(day_forecasts: list[dict]) -> dict[str, Any]:
    """Вычисляет усредненные данные за день."""
    if not day_forecasts:
        return {}
    
    temps = []
    humidities = []
    pressures = []
    wind_speeds = []
    descriptions = []
    
    for forecast in day_forecasts:
        main = forecast.get('main', {})
        weather = forecast.get('weather', [{}])[0]
        wind = forecast.get('wind', {})
        
        if 'temp' in main:
            temps.append(main['temp'])
        if 'humidity' in main:
            humidities.append(main['humidity'])
        if 'pressure' in main:
            pressures.append(main['pressure'])
        if 'speed' in wind:
            wind_speeds.append(wind['speed'])
        if 'description' in weather:
            descriptions.append(weather['description'])
    
    def avg(values):
        return sum(values) / len(values) if values else None
    
    return {
        'temp_avg': avg(temps),
        'temp_min': min(temps) if temps else None,
        'temp_max': max(temps) if temps else None,
        'humidity_avg': avg(humidities),
        'pressure_avg': avg(pressures),
        'wind_speed_avg': avg(wind_speeds),
        'description': max(set(descriptions), key=descriptions.count) if descriptions else 'N/A',
        'forecasts': day_forecasts
    }


def _print_daily_forecast_summary(date: str, daily_data: dict[str, Any]) -> None:
    """Выводит усредненный прогноз на день."""
    print(f"\n{date}:")
    print(f"  Температура: {daily_data.get('temp_avg', 'N/A'):.1f}ºC")
    print(f"  Диапазон: {daily_data.get('temp_min', 'N/A'):.1f}ºC / {daily_data.get('temp_max', 'N/A'):.1f}ºC")
    print(f"  Описание: {daily_data.get('description', 'N/A')}")
    print(f"  Влажность: {daily_data.get('humidity_avg', 'N/A'):.1f}%")
    print(f"  Давление: {daily_data.get('pressure_avg', 'N/A'):.1f} hPa")
    print(f"  Ветер: {daily_data.get('wind_speed_avg', 'N/A'):.1f} м/с")


def _print_detailed_day_forecast(day_forecasts: list[dict]) -> None:
    """Выводит детальный прогноз на день с шагом 3 часа."""
    print("\nДетальный прогноз (шаг 3 часа):")
    print("-" * 60)
    
    for forecast in day_forecasts:
        dt_txt = forecast.get('dt_txt', 'N/A')
        main = forecast.get('main', {})
        weather = forecast.get('weather', [{}])[0]
        wind = forecast.get('wind', {})
        
        print(f"\n{dt_txt}:")
        print(f"  Температура: {main.get('temp', 'N/A')}ºC")
        print(f"  Ощущается как: {main.get('feels_like', 'N/A')}ºC")
        print(f"  Описание: {weather.get('description', 'N/A')}")
        print(f"  Влажность: {main.get('humidity', 'N/A')}%")
        print(f"  Давление: {main.get('pressure', 'N/A')} hPa")
        
        wind_speed = wind.get('speed', 'N/A')
        wind_gust = wind.get('gust', None)
        wind_deg = wind.get('deg', None)
        
        wind_info = f"  Ветер: {wind_speed} м/с"
        if wind_gust is not None:
            wind_info += f", порывы до {wind_gust} м/с"
        if wind_deg is not None:
            wind_info += f", направление {_get_wind_direction(wind_deg)} ({wind_deg}°)"
        print(wind_info)
        
        pop = forecast.get('pop', None)
        if pop is not None:
            print(f"  Вероятность осадков: {pop * 100:.0f}%")


def _get_wind_direction(deg: Optional[int]) -> str:
    """Преобразует направление ветра из градусов в читаемый формат."""
    if deg is None:
        return "N/A"
    directions = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    index = round(deg / 45) % 8
    return directions[index]


def _format_sun_time(utc_timestamp: int, tz_offset_seconds: int) -> str:
    """
    Форматирует время восхода/захода в местное время по координатам.
    UTC (Unix) из API преобразуется в местный часовой пояс: смещение в секундах
    задаётся в ответе погоды на корневом уровне (timezone) для данного места.
    """
    utc_dt = datetime.fromtimestamp(utc_timestamp, tz=timezone.utc)
    local_tz = timezone(timedelta(seconds=tz_offset_seconds))
    local_dt = utc_dt.astimezone(local_tz)
    return local_dt.strftime("%H:%M")


def _print_weather(weather: dict[str, Any]) -> None:
    try:
        main = weather['main']
        wind = weather.get('wind', {})
        coord = weather.get('coord', {})
        sys_data = weather.get('sys', {})
        
        # Основная информация
        print(f"Погода в {weather['name']}: {main['temp']}ºC, {weather['weather'][0]['description']}")
        
        # Влажность и давление
        print(f"Влажность: {main.get('humidity', 'N/A')}%")
        print(f"Давление: {main.get('pressure', 'N/A')} hPa")
        
        # Ветер
        wind_speed = wind.get('speed', 'N/A')
        wind_gust = wind.get('gust', None)
        wind_deg = wind.get('deg', None)
        
        wind_info = f"Ветер: {wind_speed} м/с"
        if wind_gust is not None:
            wind_info += f", порывы до {wind_gust} м/с"
        if wind_deg is not None:
            wind_info += f", направление {_get_wind_direction(wind_deg)} ({wind_deg}°)"
        
        print(wind_info)
        
        # Восход и заход солнца в местном времени (timezone — на корневом уровне ответа API)
        tz_offset = weather.get('timezone', 0) or sys_data.get('timezone', 0)
        sunrise = sys_data.get('sunrise')
        sunset = sys_data.get('sunset')
        if sunrise is not None and sunset is not None:
            sunrise_local = _format_sun_time(sunrise, tz_offset)
            sunset_local = _format_sun_time(sunset, tz_offset)
            print(f"Восход солнца: {sunrise_local}, Заход солнца: {sunset_local}")
        
        # Индекс качества воздуха (AQI)
        lat = coord.get('lat')
        lon = coord.get('lon')
        if lat is not None and lon is not None:
            pollution_data = get_air_pollution(lat, lon)
            if pollution_data:
                analysis = analyze_air_pollution(pollution_data, extended=False)
                aqi = analysis.get('aqi')
                level_name = analysis.get('level_name', 'N/A')
                print(f"Индекс качества воздуха (AQI): {aqi} — {level_name}")
                exceeded = analysis.get("exceeded_parameters", [])
                if exceeded:
                    print("  Параметры, превышающие норму:")
                    for item in exceeded:
                        print(f"    {item['component']}: {item['value']} µg/m³ (норма до {item['threshold']})")
    except Exception as e:
        print(f"Ошибка при выводе погоды: {e}")
        print(weather)


def _fetch_weather_interactive(
    *,
    city: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
) -> Optional[dict[str, Any]]:
    try:
        if city is not None:
            return get_current_weather(city)
        if lat is not None and lon is not None:
            return get_current_weather(latitude=lat, longitude=lon)
        return None
    except TransientRequestError as e:
        cache = _load_cache()
        if (
            cache
            and isinstance(cache.get("fetched_at"), str)
            and _is_fresh(cache["fetched_at"])
            and _cache_matches_request(cache, city=city, lat=lat, lon=lon)
        ):
            answer = input(
                "Сетевая ошибка при получении погоды. Показать данные из кэша (свежее 3 часов)? [Y/n]: "
            ).strip().lower()
            if answer in ("", "y", "yes", "д", "да"):
                data = cache.get("data")
                return data if isinstance(data, dict) else None
        else:
            print(f"Сетевая ошибка: {e}")
        return None


def _prompt_city(default: Optional[str] = None) -> Optional[str]:
    prompt = "Введите город"
    if default:
        prompt += f" (Enter = {default})"
    prompt += ": "
    value = input(prompt).strip()
    if not value:
        return default
    return value


def _prompt_float(prompt: str) -> Optional[float]:
    value = input(prompt).strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _show_cache() -> None:
    cache = _load_cache()
    if not cache:
        print("Кэш не найден.")
        return

    fetched_at = cache.get("fetched_at")
    is_fresh = isinstance(fetched_at, str) and _is_fresh(fetched_at)
    city = cache.get("city")
    lat = cache.get("lat")
    lon = cache.get("lon")

    print("Кэш:")
    print(f"- city: {city}")
    print(f"- lat/lon: {lat}, {lon}")
    print(f"- fetched_at: {fetched_at}")
    print(f"- fresh(<3h): {'yes' if is_fresh else 'no'}")

    data = cache.get("data")
    if isinstance(data, dict):
        _print_weather(data)
    else:
        print("(Нет данных погоды в кэше)")


def _show_forecast_interactive(city: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None) -> None:
    """Интерактивный показ 5-дневного прогноза."""
    # Получаем координаты
    if city:
        coords = get_coordinates(city)
        if not coords:
            print("Не удалось получить координаты города.")
            return
        lat, lon = coords
    elif lat is None or lon is None:
        print("Не указаны координаты или город.")
        return
    
    # Получаем прогноз
    print(f"\nПолучаем прогноз на 5 дней...")
    forecast_list = get_forecast_5d3h(lat, lon)
    
    if not forecast_list:
        print("Не удалось получить прогноз.")
        return
    
    # Группируем по дням
    grouped = _group_forecast_by_days(forecast_list)
    dates = sorted(grouped.keys())
    
    if not dates:
        print("Нет данных прогноза.")
        return
    
    # Выводим усредненный прогноз по дням
    print("\n=== Прогноз на 5 дней (усредненные данные) ===")
    daily_averages = {}
    for i, date in enumerate(dates[:5], 1):  # Максимум 5 дней
        daily_data = _calculate_daily_average(grouped[date])
        daily_averages[date] = daily_data
        print(f"{i}. ", end="")
        _print_daily_forecast_summary(date, daily_data)
    
    # Предлагаем выбрать день для детального просмотра
    print("\n" + "-" * 60)
    print("Введите номер дня (1-5) для детального прогноза с шагом 3 часа")
    print("Или нажмите Enter для выхода")
    
    choice = input("Выбор: ").strip()
    if choice and choice.isdigit():
        day_num = int(choice)
        if 1 <= day_num <= len(dates):
            selected_date = dates[day_num - 1]
            day_forecasts = grouped[selected_date]
            _print_detailed_day_forecast(day_forecasts)
        else:
            print("Неверный номер дня.")


def _print_air_pollution(analysis: dict) -> None:
    """Выводит результаты анализа загрязнения воздуха в терминал."""
    print("\n=== Загрязнение воздуха ===")
    print(f"Статус: {analysis.get('status', 'N/A')}")
    print(f"Индекс качества воздуха (AQI): {analysis.get('aqi', 'N/A')}")
    print(f"Уровень: {analysis.get('level_name', 'N/A')}")
    
    exceeded = analysis.get("exceeded_parameters", [])
    if exceeded:
        print("\nПараметры, превышающие допустимые значения:")
        for item in exceeded:
            print(f"  {item['component']}: {item['value']} µg/m³ (норма до {item['threshold']}, превышение +{item['excess']})")
    
    if analysis.get("components"):
        print("\nКомпоненты (µg/m³):")
        for key, value in analysis["components"].items():
            print(f"  {key}: {value}")


def _show_air_pollution_interactive(city: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None) -> None:
    """Получает и выводит данные о загрязнении воздуха."""
    if city:
        print(f"Получаем координаты для города: {city}")
        coords = get_coordinates(city)
        if not coords:
            print("Не удалось получить координаты города.")
            return
        lat, lon = coords
    elif lat is None or lon is None:
        print("Не указаны координаты или город.")
        return
    
    print("Получаем данные о загрязнении воздуха...")
    pollution_data = get_air_pollution(lat, lon)
    
    if not pollution_data:
        print("Не удалось получить данные о загрязнении воздуха.")
        return
    
    analysis = analyze_air_pollution(pollution_data, extended=True)
    _print_air_pollution(analysis)


def main() -> None:
    default_city: Optional[str] = "Екатеринбург"

    while True:
        print()
        print("=== Weather CLI ===")
        print("1 — Текущая погода по городу")
        print("2 — Текущая погода по координатам")
        print("3 — Прогноз на 5 дней по городу")
        print("4 — Прогноз на 5 дней по координатам")
        print("5 — Загрязнение воздуха")
        print("6 — Показать кэш")
        print("0 — Выход")

        choice = input("Выберите режим: ").strip()
        if choice == "0":
            print("Выход.")
            return

        if choice == "1":
            city = _prompt_city(default_city)
            if not city:
                print("Город не задан.")
                continue
            default_city = city
            weather = _fetch_weather_interactive(city=city, lat=None, lon=None)
            if isinstance(weather, dict):
                _print_weather(weather)
            else:
                print("Не удалось получить погоду.")
            continue

        if choice == "2":
            lat = _prompt_float("Введите широту (lat): ")
            lon = _prompt_float("Введите долготу (lon): ")
            if lat is None or lon is None:
                print("Некорректные координаты.")
                continue
            weather = _fetch_weather_interactive(city=None, lat=lat, lon=lon)
            if isinstance(weather, dict):
                _print_weather(weather)
            else:
                print("Не удалось получить погоду.")
            continue

        if choice == "3":
            city = _prompt_city(default_city)
            if not city:
                print("Город не задан.")
                continue
            default_city = city
            _show_forecast_interactive(city=city, lat=None, lon=None)
            continue

        if choice == "4":
            lat = _prompt_float("Введите широту (lat): ")
            lon = _prompt_float("Введите долготу (lon): ")
            if lat is None or lon is None:
                print("Некорректные координаты.")
                continue
            _show_forecast_interactive(city=None, lat=lat, lon=lon)
            continue

        if choice == "5":
            sub = input("Загрязнение воздуха: по городу (1) или по координатам (2)? ").strip()
            if sub == "1":
                city = _prompt_city(default_city)
                if not city:
                    print("Город не задан.")
                    continue
                default_city = city
                _show_air_pollution_interactive(city=city, lat=None, lon=None)
            elif sub == "2":
                lat = _prompt_float("Введите широту (lat): ")
                lon = _prompt_float("Введите долготу (lon): ")
                if lat is None or lon is None:
                    print("Некорректные координаты.")
                    continue
                _show_air_pollution_interactive(city=None, lat=lat, lon=lon)
            else:
                print("Введите 1 или 2.")
            continue

        if choice == "6":
            _show_cache()
            continue

        print("Неизвестный режим. Введите 1, 2, 3, 4, 5, 6 или 0.")





if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
    



