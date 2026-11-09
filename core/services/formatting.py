"""Indonesian date/time formatting for notification texts (mirrors the frontend)."""
from datetime import date, datetime, time

DAY_SHORT = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
DAY_LONG = {"mon": "Senin", "tue": "Selasa", "wed": "Rabu", "thu": "Kamis", "fri": "Jumat", "sat": "Sabtu", "sun": "Minggu"}
WEEKDAY_KEY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTH = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def rupiah(value):
    return "Rp" + f"{int(value):,}".replace(",", ".")


def fmt_date(d: date):
    return f"{DAY_SHORT[d.weekday()]}, {d.day} {MONTH[d.month - 1]} {d.year}"


def fmt_time(t):
    if isinstance(t, str):
        return t[:5].replace(":", ".")
    return t.strftime("%H.%M")


def fmt_slot(d, start, end):
    if not d:
        return ""
    return f"{fmt_date(d)}, {fmt_time(start)}–{fmt_time(end)}"


def hhmm(t: time | str | None):
    if t is None:
        return None
    if isinstance(t, str):
        return t[:5]
    return t.strftime("%H:%M")


def parse_hhmm(value):
    try:
        return datetime.strptime(str(value)[:5], "%H:%M").time()
    except (TypeError, ValueError):
        return None
