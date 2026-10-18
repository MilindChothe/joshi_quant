"""Central config: loads .env, exposes dict + save back to .env."""
import os
from dotenv import load_dotenv, set_key, dotenv_values

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

DEFAULTS = {
    "KOTAK_CONSUMER_KEY": "",
    "KOTAK_CONSUMER_SECRET": "",
    "KOTAK_MOBILE_NUMBER": "",
    "KOTAK_UCC": "",
    "KOTAK_MPIN": "",
    "KOTAK_TOTP_SECRET": "",
    "KOTAK_ENVIRONMENT": "prod",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "MODE": "paper",
    "ENTRY_TIME": "09:16",
    "EXIT_TIME": "15:10",
    "SMA_PERIOD": "20",
    "SMA_THRESHOLD_PCT": "0.1",
    "SHORT_PREMIUM_1": "50",
    "SL_POINTS_1": "15",
    "SHORT_PREMIUM_2": "30",
    "SL_PCT_2": "25",
    "LOTS": "1",
    "NIFTY_LOT_QTY": "75",
    "EXPIRY_WEEKDAY": "1",
    "USE_NEXT_WEEKLY": "True",
    "LTP_REFRESH_SEC": "45",
    "CONSOLE_ONLY": "False",
    "PRODUCT": "MIS",
    "FLASK_PORT": "5000",
}

STRATEGY_KEYS = [
    "MODE", "ENTRY_TIME", "EXIT_TIME", "SMA_PERIOD", "SMA_THRESHOLD_PCT",
    "SHORT_PREMIUM_1", "SL_POINTS_1", "SHORT_PREMIUM_2", "SL_PCT_2",
    "LOTS", "NIFTY_LOT_QTY", "EXPIRY_WEEKDAY", "USE_NEXT_WEEKLY", "PRODUCT",
    "LTP_REFRESH_SEC", "CONSOLE_ONLY",
]

CRED_KEYS = [
    "KOTAK_CONSUMER_KEY", "KOTAK_CONSUMER_SECRET", "KOTAK_MOBILE_NUMBER",
    "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET", "KOTAK_ENVIRONMENT",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]


def ensure_env():
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w") as f:
            for k, v in DEFAULTS.items():
                f.write(f"{k}={v}\n")
    load_dotenv(ENV_PATH, override=True)


def get_config():
    ensure_env()
    cfg = dict(dotenv_values(ENV_PATH))
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    # type coercion helpers done by callers
    return cfg


def save_config(updates: dict):
    ensure_env()
    for k, v in updates.items():
        if k in DEFAULTS:
            set_key(ENV_PATH, k, str(v))
    load_dotenv(ENV_PATH, override=True)
    return get_config()


def as_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def as_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def as_bool(v):
    return str(v).lower() in ("1", "true", "yes", "y")
