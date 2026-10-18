"""Flask web dashboard for 20-SMA intraday option-selling (Kotak Neo live + paper)."""
from flask import Flask, render_template, request, jsonify
from config import get_config, save_config, as_bool
from engine import ENGINE
from brokers import KotakBroker
from datetime import datetime
import socket
import time
import sys

app = Flask(__name__)


def get_ips():
    local = "-"
    public = "-"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    try:
        import requests
        public = requests.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception as e:
        public = f"lookup failed: {e}"
    return local, public


def publish_ip(note="program started"):
    from telegram_utils import send_telegram
    cfg = get_config()
    local, public = get_ips()
    ENGINE.state["ip_local"] = local
    ENGINE.state["ip_public"] = public
    msg = (f"<b>20-SMA {note}</b>\nPublic IP: <code>{public}</code>\n"
           f"Local IP: <code>{local}</code>\n{datetime.now().strftime('%d %H:%M:%S')}")
    res = send_telegram(cfg.get("TELEGRAM_BOT_TOKEN", ""), cfg.get("TELEGRAM_CHAT_ID", ""), msg)
    ENGINE.log(f"IP published ({note}): public={public} local={local} "
               f"{'telegram OK' if res.get('ok') else res.get('error', 'telegram skipped')}")
    return local, public


@app.route("/")
def index():
    return render_template("index.html", cfg=get_config(), state=ENGINE.state)


@app.route("/api/status")
def status():
    st = ENGINE.state
    open_pnl = round(sum(p.get("pnl", 0) for p in st.get("positions", [])), 2)
    closed = st.get("closed", [])
    realised = round(ENGINE.paper.realized, 2)
    credit = sum(p.get("entry", 0) * abs(p.get("qty", 0)) for p in st.get("positions", []))
    credit += sum(c.get("entry", 0) * abs(c.get("qty", 0)) for c in closed)
    debit = sum(c.get("exit", 0) * abs(c.get("qty", 0)) for c in closed)
    summary = {
        "unrealised": open_pnl,
        "realised": realised,
        "net": round(realised + open_pnl, 2),
        "traded_value": round(credit - debit, 2),
    }
    return jsonify({"cfg": get_config(), "state": st, "summary": summary})


@app.route("/api/save", methods=["POST"])
def save():
    data = request.get_json(force=True) or {}
    cfg = save_config({k: v for k, v in data.items()})
    ENGINE.log("settings saved from web")
    return jsonify({"ok": True, "cfg": cfg})


@app.route("/api/start", methods=["POST"])
def start():
    publish_ip("strategy started")
    ENGINE.start(get_config())
    return jsonify({"ok": True, "state": ENGINE.state})


@app.route("/api/stop", methods=["POST"])
def stop():
    ENGINE.stop()
    # square-off paper on manual stop? keep positions visible
    return jsonify({"ok": True})


@app.route("/api/squareoff", methods=["POST"])
def squareoff():
    n = len(ENGINE.paper.positions)
    ENGINE._refresh_live_ltps(max_age_s=0)
    shut = ENGINE.paper.close_all()
    for s in shut:
        if s:
            s["exit_reason"] = "manual"
    ENGINE.record_closed(shut, "manual")
    if get_config().get("MODE", "paper") == "live":
        ENGINE._live_cover_all(get_config(), shut, "manual")
    ENGINE.state["positions"] = []
    ENGINE.state["day_pnl"] = ENGINE.paper.realized
    ENGINE.log(f"manual square-off {n} legs")
    return jsonify({"ok": True})


@app.route("/api/kotak_login", methods=["POST"])
def kotak_login():
    data = request.get_json(force=True) or {}
    totp = str(data.get("totp", "")).strip()
    # allow saving creds inline
    creds = {k: data[k] for k in
             ["KOTAK_CONSUMER_KEY","KOTAK_CONSUMER_SECRET","KOTAK_MOBILE_NUMBER",
              "KOTAK_UCC","KOTAK_MPIN","KOTAK_TOTP_SECRET"] if k in data}
    if creds:
        save_config(creds)
    ok, msg = ENGINE.kotak_login(totp)
    return jsonify({"ok": ok, "msg": msg, "funds": ENGINE.state.get("funds", {}),
                    "funds_summary": ENGINE.state.get("funds_summary","-")})


def run_test_order(cfg, totp_code: str = ""):
    """Send ONE sample live order to Kotak Neo and exit. Usage: python app.py --test [TOTP]
    Places a limit SELL on the cheapest CE <= 50 premium (next weekly expiry)
    plus its broker-native SL-BUY at entry + SL_POINTS_1."""
    print("=== 20-SMA LIVE ORDER TEST ===")
    print("WARNING: this places a REAL order via Kotak Neo. Press Ctrl+C to abort now.")
    time.sleep(2)

    kb = KotakBroker(cfg)
    ok, msg = kb.login(totp_code)
    print(f"Kotak login: {'OK' if ok else 'FAILED'} - {msg}")
    if not ok:
        sys.exit(1)

    try:
        limits = kb.client.limits(segment="ALL", exchange="ALL", product="ALL")
        avail = (limits.get("availableMargin") if isinstance(limits, dict) else None) or str(limits)[:120]
        print(f"Available margin/cash: {avail}")
    except Exception as e:
        print(f"funds fetch skipped: {e}")

    from option_chain import resolve_live_expiry, fetch_chain_v3, pick_strikes, spot_from_chain
    from datetime import date as _date

    expiry = resolve_live_expiry(True) or resolve_expiry(_date.today(), 1, True)
    print(f"expiry: {expiry}")

    ch, _ = fetch_chain_v3("NIFTY", expiry)
    spot = spot_from_chain(ch)
    print(f"spot: {spot}")

    picks = pick_strikes(ch, 50.0)
    leg = picks["CE"] or picks["PE"]
    if not leg:
        print("no strike <= 50 found in chain; test aborted (no order placed)")
        sys.exit(1)
    strike, px = leg
    opt = "CE" if picks["CE"] else "PE"
    print(f"selected {opt} {strike} @ {px} (target <= 50)")

    qty = int(float(cfg.get("NIFTY_LOT_QTY", 65))) * int(float(cfg.get("LOTS", 1)))
    prod = cfg.get("PRODUCT", "MIS")
    sl_pts = float(cfg.get("SL_POINTS_1", 15))

    sym = kb.find_option_symbol(strike, opt, expiry)
    print(f"broker symbol: {sym}")
    if not sym:
        print("symbol lookup returned nothing - aborting to avoid wrong instrument")
        sys.exit(1)
    if " " in str(sym):
        print("WARNING: symbol has spaces - verify this format in Kotak Neo before order lands")

    limit_px = max(round(px - 0.5, 1), 0.5)
    sell_res = kb.place_short(sym, qty, prod, limit_price=limit_px)
    print(f"SELL limit {sym} qty {qty} @ {limit_px} -> {sell_res}")

    if isinstance(sell_res, dict) and sell_res.get("Error") or (isinstance(sell_res, dict) and "error" in str(sell_res).lower()):
        print("SELL rejected by broker - SL not placed, test aborted")
        sys.exit(1)

    sl_res = kb.place_sl_for_short(sym, qty, px, prod, sl_points=sl_pts)
    print(f"SL-BUY trigger {sl_res['trigger']} limit {sl_res['limit']} -> {sl_res['resp']}")

    tg = (
        f"<b>20-SMA LIVE ORDER TEST</b>\n"
        f"SELL {opt} {strike} @ {limit_px} qty {qty} expiry {expiry}\n"
        f"SL trigger {sl_res['trigger']} limit {sl_res['limit']}\n"
        f"{datetime.now().strftime('%d %b %H:%M:%S')}"
    )
    from telegram_utils import send_telegram
    res = send_telegram(cfg.get("TELEGRAM_BOT_TOKEN", ""), cfg.get("TELEGRAM_CHAT_ID", ""), tg)
    print(f"telegram: {'OK' if res.get('ok') else res.get('error','skipped')}")
    print("=== TEST DONE - check Kotak Neo order book ===")


if __name__ == "__main__":
    from config import ensure_env, get_config, as_bool
    ensure_env()
    cfg = get_config()
    publish_ip("program started")

    if "--test" in sys.argv:
        totp = sys.argv[2] if len(sys.argv) > 2 else ""
        run_test_order(cfg, totp)
        sys.exit(0)

    if as_bool(cfg.get("CONSOLE_ONLY", False)):
        print("20-SMA console-only mode (Ctrl+C to stop)")
        if not ENGINE.running:
            ENGINE.start(cfg)
        try:
            while True:
                st = ENGINE.state
                summ = {
                    "unrealised": round(sum(p.get("pnl", 0) for p in st.get("positions", [])), 2),
                    "realised": round(ENGINE.paper.realized, 2),
                }
                summ["net"] = round(summ["unrealised"] + summ["realised"], 2)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"mode={st['mode']} signal={st['signal']} spot={st['spot']:.1f} "
                      f"sma={st['sma']:.1f} "
                      f"unrealised={summ['unrealised']:.1f} realised={summ['realised']:.1f} net={summ['net']:.1f} "
                      f"legs={len(st['positions'])} "
                      f"telegram={st['last_telegram']}")
                for p in st.get("positions", []):
                    print(f"   {p['symbol']} entry={p['entry']} ltp={p.get('ltp','-')} "
                          f"pnl={p.get('pnl',0):.1f} tag={p.get('tag','')} "
                          f"entry_time={p.get('entry_time','-')}")
                time.sleep(5)
        except KeyboardInterrupt:
            ENGINE.stop()
            if as_bool(cfg.get("MODE", "paper")) == "live" and ENGINE.kotak and ENGINE.kotak.logged_in and ENGINE.paper.positions:
                print("\nCtrl+C: square-off live positions before exit...")
                shut = ENGINE.paper.close_all()
                for s in shut:
                    if s:
                        s["exit_reason"] = "manual-stop"
                ENGINE.record_closed(shut, "manual-stop")
                ENGINE._live_cover_all(cfg, shut, "manual-stop")
                print(f"square-off {len(shut)} legs sent - verify in Kotak Neo")
            print("\nstopped")
    else:
        port = int(cfg.get("FLASK_PORT", "5000") or 5000)
        print(f"20-SMA dashboard: http://127.0.0.1:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
