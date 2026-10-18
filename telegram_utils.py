"""Telegram helper - sendMessage."""
import requests


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: int = 10) -> dict:
    if not bot_token or not chat_id:
        return {"ok": False, "error": "telegram not configured"}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=timeout,
        )
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def positions_text(state: dict) -> str:
    mode = state.get("mode", "?").upper()
    sig = state.get("signal", "-")
    mtm = state.get("day_pnl", 0.0)
    lines = [f"<b>20-SMA [{mode}]</b> Signal: <b>{sig}</b>  Day P/L: <b>{mtm:.1f}</b>"]
    poss = state.get("positions", [])
    if not poss:
        lines.append("No open positions.")
    for p in poss:
        stale = "" if p.get("live_ltp", True) else " *stale"
        oid = p.get("broker_order_id") or ""
        sloid = p.get("sl_order_id") or ""
        oid_txt = f" oid:{oid}" if oid else ""
        sloid_txt = f" sl:{sloid}" if sloid else ""
        lines.append(
            f"{p.get('symbol')} {p.get('side')} Qty:{p.get('qty')} "
            f"In:{p.get('entry',0):.1f}@{p.get('entry_time', p.get('time','-'))} "
            f"LTP:{p.get('ltp',0):.1f}{stale} P/L:{p.get('pnl',0):.1f}{oid_txt}{sloid_txt}"
        )
    lines.append(f"Spot: {state.get('spot',0):.1f} | {state.get('note','')}")
    return "\n".join(lines)
