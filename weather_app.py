import requests
from dotenv import load_dotenv
import os
import json
import time
from datetime import datetime, timezone
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
        print(f"Ошибка: {response.status_code}")
        return None

def get_weather_by_coordinates(latitude: float, longitude: float) -> dict:
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={API_KEY}&units=metric&lang=ru"
    response = _request_with_retries(url)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Ошибка: {response.status_code}")
        return None


def _print_weather(weather: dict[str, Any]) -> None:
    try:
        print(f"Погода в {weather['name']}: {weather['main']['temp']}ºC, {weather['weather'][0]['description']}")
    except Exception:
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


def main() -> None:
    default_city: Optional[str] = "Екатеринбург"

    while True:
        print()
        print("=== Weather CLI ===")
        print("1 — По городу")
        print("2 — По координатам")
        print("3 — Показать кэш")
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
            _show_cache()
            continue

        print("Неизвестный режим. Введите 1, 2, 3 или 0.")





if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
    



