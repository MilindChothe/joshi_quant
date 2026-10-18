"""Weekly expiry resolver + strike rounding for NIFTY."""
from datetime import date, datetime, timedelta


def next_weekday(from_date: date, weekday: int) -> date:
    """weekday: Monday=0 .. Sunday=6. Returns upcoming weekday (strictly future if today is weekday? no - today)."""
    days_ahead = (weekday - from_date.weekday()) % 7
    return from_date + timedelta(days=days_ahead)


def resolve_expiry(today=None, expiry_weekday: int = 1, use_next_weekly: bool = True) -> date:
    """
    NIFTY weekly expiry is Tuesday (weekday=1) since Sep-2025.
    use_next_weekly=True -> skip 0DTE week: if upcoming expiry is <=2 days
    away, sell the one after. Else upcoming Tuesday IS the next weekly.
    Wed 14 Oct -> Tue 20 Oct (6 DTE), not 27 Oct.
    """
    today = today or date.today()
    upcoming = next_weekday(today, expiry_weekday)
    if upcoming == today:
        upcoming += timedelta(days=7)  # expiry-day afternoon: that expiry is gone
    if use_next_weekly and (upcoming - today).days <= 2:
        return upcoming + timedelta(days=7)
    return upcoming


def round_strike(price: float, step: int = 50) -> int:
    return int(round(price / step) * step)


def expiry_str(d: date) -> str:
    return d.strftime("%d%b%y").upper()  # e.g. 21OCT25
