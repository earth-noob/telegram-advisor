import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

_UNIT_GROUP = r"мин(?:ут\w*)?|час\w*|ч\b|день|дн\w*|д\b|недел\w*|нед\b"

_DELAY_PHRASE_RE = re.compile(
    r"(?i)\bчерез\s+(?:(?P<num>\d+(?:[.,]\d+)?|пол)\s*)?(?P<unit>" + _UNIT_GROUP + r")"
)
_DELAY_EXTEND_RE = re.compile(
    r"(?i)\s+(?:(?P<num>\d+(?:[.,]\d+)?|пол)\s*)?(?P<unit>" + _UNIT_GROUP + r")"
)


def get_tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def now() -> datetime:
    """Текущее время в основном часовом поясе (Europe/Moscow)."""
    return datetime.now(get_tz())


def utcnow() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(timezone.utc)


def utc_after(delay: timedelta) -> datetime:
    """Абсолютное время: now() в UTC + delay."""
    return utcnow() + delay


def format_dt(dt: datetime) -> str:
    """Форматирует время в основном часовом поясе."""
    return dt.astimezone(get_tz()).strftime("%d.%m.%Y %H:%M")


def parse_daily_post_time(value: str) -> tuple[int, int]:
    """'09:00' -> (9, 0)."""
    hour, minute = value.split(":")
    return int(hour), int(minute)


def seconds_until_daily(post_time: str) -> float:
    """Секунды от now() до ближайшего наступления HH:MM в config.TIMEZONE.

    Если время сегодня уже прошло (или наступило) — до завтрашнего.
    """
    hour, minute = parse_daily_post_time(post_time)
    now_local = now()
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    return (target - now_local).total_seconds()


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Разбивает длинный текст на части для Telegram."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def _pair_seconds(num: str | None, unit: str) -> float:
    if num is None:
        n = 1.0
    elif num.lower() == "пол":
        n = 0.5
    else:
        n = float(num.replace(",", "."))
    u = unit.lower()
    if u.startswith("мин"):
        return n * 60.0
    if u.startswith("час") or u == "ч":
        return n * 3600.0
    if u.startswith("нед"):
        return n * 7 * 86400.0
    if u.startswith("дн") or u in ("день", "д"):
        return n * 86400.0
    return 0.0


def extract_delay(text: str) -> tuple[timedelta | None, str]:
    """Ищет в тексте фразу 'через ...' (в любом месте, любой регистр).

    Возвращает (timedelta | None, текст без этой фразы).
    Если фразы нет или итоговый интервал <= 0 — (None, исходный текст).
    """
    m = _DELAY_PHRASE_RE.search(text)
    if not m:
        return None, text
    seconds = _pair_seconds(m.group("num"), m.group("unit"))
    end = m.end()
    while True:
        extra = _DELAY_EXTEND_RE.match(text, end)
        if not extra:
            break
        seconds += _pair_seconds(extra.group("num"), extra.group("unit"))
        end = extra.end()
    if seconds <= 0:
        return None, text
    cleaned = text[: m.start()] + " " + text[end:]
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return timedelta(seconds=round(seconds)), cleaned
