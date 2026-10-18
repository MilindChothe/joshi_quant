"""Background trading engine: 20-SMA signal + paper/live order handling + 15-min telegram."""
import threading
import time
from datetime import datetime, date
import pandas as pd

from config import get_config, as_float, as_int, as_bool
from strategy import sma_signal
from expiry_utils import resolve_expiry
from brokers import PaperBroker, KotakBroker
from telegram_utils import send_telegram, positions_text


class Engine:
    def __init__(self):
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.state = {
            "running": False, "mode": "paper", "signal": "FLAT",
            "prev_signal": "FLAT", "spot": 0.0, "sma": 0.0,
            "positions": [], "closed": [], "day_pnl": 0.0, "realized": 0.0,
            "note": "idle", "logs": [], "last_telegram": "-",
            "expiry": "-", "kotak_logged_in": False, "kotak_msg": "",
            "funds": {}, "funds_summary": "-",
        }
        self.paper = PaperBroker()
        self.kotak = None
        self._last_tg = 0.0
        self._last_cycle = ""

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.state["logs"] = (self.state["logs"] + [f"[{ts}] {msg}"])[-80:]

    def record_closed(self, items, reason=""):
        with self.lock:
            for it in items or []:
                if not it:
                    continue
                it = dict(it)
                if reason and not it.get("exit_reason"):
                    it["exit_reason"] = reason
                self.state["closed"] = ([it] + self.state.get("closed", []))[:50]

    # ---------- data ----------
    def fetch_spot_df(self):
        """NIFTY 5-min closes via yfinance (^NSEI). Returns (None, None) when down -
        caller must skip the cycle. NEVER fabricate candles: fake spot would trade real money."""
        try:
            import yfinance as yf
            df = yf.download("^NSEI", period="2d", interval="5m",
                             progress=False, auto_adjust=True)
            if df is None or len(df) < 25:
                raise RuntimeError("not enough candles")
            closes = df["Close"]
            if hasattr(closes, "iloc") and getattr(closes, "ndim", 1) == 2:
                closes = closes.iloc[:, 0]
            out = pd.DataFrame({"close": [float(x) for x in closes.values]})
            spot = float(out["close"].iloc[-1])
            return out, spot
        except Exception as e:
            self.log(f"spot feed down, cycle skipped: {e}")
            return None, None

    # ---------- control ----------
    def start(self, cfg):
        self.paper = PaperBroker(lot_qty=as_int(cfg.get("NIFTY_LOT_QTY", 75), 75),
                                 lots=as_int(cfg.get("LOTS", 1), 1))
        self.state["realized"] = 0.0
        self.state["closed"] = []
        self.state["positions"] = []
        self._last_ltp = 0.0
        self._last_tg = 0.0  # heartbeat immediately on start
        if not self.running:
            self.running = True
            self.state["running"] = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.log(f"started in {cfg.get('MODE','paper')} mode")
        else:
            self.log("already running")

    def stop(self):
        self.running = False
        self.state["running"] = False
        self.log("stopped")

    def kotak_login(self, totp_code=""):
        cfg = get_config()
        self.kotak = KotakBroker(cfg)
        ok, msg = self.kotak.login(totp_code)
        self.state["kotak_logged_in"] = ok
        self.state["kotak_msg"] = msg
        self.state["funds"] = {}
        self.log(f"Kotak login: {msg}")
        if ok:
            try:
                limits = self.kotak.client.limits(segment="ALL", exchange="ALL", product="ALL")
                self.state["funds"] = limits if isinstance(limits, dict) else {"raw": str(limits)}
                avail = (limits.get("availableMargin") if isinstance(limits, dict) else None) or \
                        (limits.get("cash") if isinstance(limits, dict) else None) or \
                        str(limits)[:120]
                self.state["funds_summary"] = str(avail)
                tg = (f"<b>Kotak Neo LOGIN OK</b>\n"
                      f"Mode: {cfg.get('MODE','paper').upper()}\n"
                      f"Available margin/cash: {avail}\n"
                      f"{datetime.now().strftime('%d %b %H:%M:%S')}")
                res = send_telegram(cfg.get("TELEGRAM_BOT_TOKEN",""),
                                    cfg.get("TELEGRAM_CHAT_ID",""), tg)
                self.log(f"Kotak login OK | funds fetched | telegram {'OK' if res.get('ok') else res.get('error','skipped')}")
            except Exception as e:
                self.log(f"Kotak login OK but funds fetch failed: {e}")
        else:
            self.log(f"Kotak login FAILED: {msg}")
        return ok, msg

    # ---------- main loop ----------
    def _loop(self):
        while self.running:
            try:
                self._cycle()
            except Exception as e:
                self.log(f"cycle error: {e}")
            time.sleep(30)
        self.state["running"] = False

    def _cycle(self):
        cfg = get_config()
        mode = str(cfg.get("MODE", "paper")).lower()
        try:
            entry_t = datetime.strptime(cfg.get("ENTRY_TIME", "09:16"), "%H:%M").time()
            exit_t = datetime.strptime(cfg.get("EXIT_TIME", "15:10"), "%H:%M").time()
        except Exception:
            entry_t = datetime.strptime("09:16", "%H:%M").time()
            exit_t = datetime.strptime("15:10", "%H:%M").time()
            self.log("invalid ENTRY_TIME/EXIT_TIME in config - using 09:16/15:10")
        now_t = datetime.now().time()
        period = as_int(cfg.get("SMA_PERIOD", 20), 20)
        thr = as_float(cfg.get("SMA_THRESHOLD_PCT", 0.1), 0.1)

        df, spot = self.fetch_spot_df()
        if df is None:
            with self.lock:
                self.state["note"] = "spot feed down, waiting"
            return
        sma = float(df["close"].rolling(period).mean().iloc[-1]) if len(df) >= period else 0.0
        raw = sma_signal(df, period, thr)

        with self.lock:
            self.state["spot"] = spot
            self.state["sma"] = round(sma, 2)
            self.state["mode"] = mode

        # resolve signal (HOLD keeps previous)
        if raw != "HOLD":
            new_signal = raw
        else:
            new_signal = self.state.get("signal", "FLAT")
            if new_signal not in ("LONG", "SHORT"):
                new_signal = self.state.get("prev_signal", "FLAT")

        in_session = (entry_t <= now_t <= exit_t)

        # time exit
        if now_t >= exit_t and self.paper.positions:
            ok = self._refresh_live_ltps(max_age_s=0)
            closed = self.paper.close_all()
            for s in closed:
                if s:
                    s["exit_reason"] = "time·LIVE" if ok else "time·LAST"
            self.record_closed(closed)
            if mode == "live" and self.kotak and self.kotak.logged_in:
                self._live_cover_all(cfg, closed, "exit-time")
            self.state["realized"] = self.paper.realized
            mtm = self.paper.realized
            self.state["day_pnl"] = mtm
            self.state["positions"] = []
            self.log(f"EXIT-TIME square-off {len(closed)} legs. P/L {mtm:.1f}")
            self._telegram(cfg, force=True, note="Exit-time square-off")
            with self.lock:
                self.state["signal"] = "FLAT"
            return

        if not in_session:
            with self.lock:
                self.state["note"] = f"outside {cfg.get('ENTRY_TIME')}-{cfg.get('EXIT_TIME')}"
                if not self.paper.positions and now_t >= exit_t:
                    # flat after close: reset so next morning's signal re-arms entry
                    self.state["signal"] = "FLAT"
                    self.state["prev_signal"] = "FLAT"
                else:
                    self.state["signal"] = new_signal if self.state.get("signal") in ("LONG","SHORT") else "FLAT"
            if self.paper.positions:
                self._maybe_telegram(cfg)
            return

        # Live-only MTM: LTPs come from NSE chain prints, never a model.
        # Refresh at most once per 45s (NSE throttles faster polling).
        mtm = self.paper.update_mtm(spot)
        if self.paper.positions:
            self._refresh_live_ltps()
            mtm = self.paper.realized + sum(p.get("pnl", 0) for p in self.paper.positions)
        sl1 = as_float(cfg.get("SL_POINTS_1", 15), 15)
        sl2 = as_float(cfg.get("SL_PCT_2", 25), 25)
        for p in list(self.paper.positions):
            tag = p.get("tag", "")
            if "LEG1" in tag:
                hit = self.paper.check_sl(p, sl_points=sl1)
            else:
                hit = self.paper.check_sl(p, sl_pct=sl2)
            if hit:
                # Confirm on a fresh live print (never close on a stale/model quote).
                # Hold position if chain blocked.
                exit_src = "LAST"
                try:
                    from option_chain import fetch_chain_v3 as _fc, ltps_for as _lt
                    from datetime import datetime as _dt
                    _exp = _dt.strptime(p["expiry"], "%Y-%m-%d").date()
                    _ch, _ = _fc("NIFTY", _exp)
                    _live = _lt(_ch).get((p.get("strike"), p.get("opt")))
                    if _live:
                        p["ltp"] = _live
                        p["pnl"] = round((p["entry"] - _live) * abs(p["qty"]), 2)
                        exit_src = "LIVE"
                        # re-check breach on live print before closing
                        hit = self.paper.check_sl(p, sl_points=sl1 if "LEG1" in tag else None,
                                                  sl_pct=None if "LEG1" in tag else sl2)
                        if not hit:
                            self.log(f"SL unconfirmed by live ({p['symbol']} live {_live}), holding")
                            continue
                    else:
                        self.log(f"SL unconfirmed, no live print for {p['symbol']}, holding")
                        continue
                except Exception as e:
                    self.log(f"SL unconfirmed, chain blocked ({str(e)[:80]}), holding")
                    continue
                shut = self.paper.close(p["id"], reason=f"SL·{exit_src}")
                self.record_closed([shut], f"SL·{exit_src}")
                self.log(f"SL hit, exited {p['symbol']} @ {p['ltp']:.1f} [{exit_src}]")
                if mode == "live" and self.kotak and self.kotak.logged_in:
                    # Broker SL-BUY placed at entry should have covered this leg.
                    # Cancel only THIS leg's tracked SL (never the surviving leg's),
                    # and do NOT fire a duplicate cover that could flip net long.
                    try:
                        sym = p.get("broker_sym") or self._broker_sym_for(p, expiry_days=p.get("expiry"))
                        res = self.kotak.cancel_sl(sym) if sym else {"skipped": "no symbol"}
                        self.log(f"live: leg SL {res}; broker fill should match - VERIFY in Neo")
                    except Exception as e:
                        self.log(f"live SL cleanup failed: {e}")

        # reversal entry
        prev = self.state.get("signal", "FLAT")
        if new_signal in ("LONG", "SHORT") and new_signal != prev:
            # exit opposite
            if self.paper.positions:
                self._refresh_live_ltps(max_age_s=0)
                shut = self.paper.close_all()
                for s in shut:
                    if s:
                        s["exit_reason"] = "reversal"
                self.record_closed(shut, "reversal")
                self.log(f"reversal {prev}->{new_signal}: old legs closed")
                if mode == "live":
                    self._live_cover_all(cfg, shut, "reversal")
            self._enter_legs(new_signal, spot, cfg)

        with self.lock:
            self.state["prev_signal"] = prev
            self.state["signal"] = new_signal
            self.state["positions"] = [dict(p) for p in self.paper.positions]
            self.state["realized"] = self.paper.realized
            self.state["day_pnl"] = self.paper.update_mtm(spot)
            self.state["note"] = f"in session, SMA {sma:.1f}"
        self._maybe_telegram(cfg)

    def _refresh_live_ltps(self, max_age_s=None):
        """Overwrite open-leg LTPs with NSE chain prints. Returns True if refreshed."""
        if not self.paper.positions:
            return False
        if max_age_s is None:
            try:
                max_age_s = max(int(float(get_config().get("LTP_REFRESH_SEC", 45))), 15)
            except Exception:
                max_age_s = 45
        if max_age_s and time.time() - getattr(self, "_last_ltp", 0) < max_age_s:
            return True  # recent enough
        try:
            from option_chain import fetch_chain_v3, ltps_for
            from datetime import datetime as _dt
            exp_obj = _dt.strptime(self.paper.positions[0]["expiry"], "%Y-%m-%d").date()
            ch, _ = fetch_chain_v3("NIFTY", exp_obj)
            ltps = ltps_for(ch)
            n = 0
            for p in self.paper.positions:
                key = (p.get("strike"), p.get("opt"))
                if key in ltps:
                    p["ltp"] = ltps[key]
                    p["pnl"] = round((p["entry"] - p["ltp"]) * abs(p["qty"]), 2)
                    p["live_ltp"] = True
                    n += 1
            self._last_ltp = time.time()
            if n:
                with self.lock:
                    self.state["ltp_time"] = datetime.now().strftime("%H:%M:%S")
                self.log(f"LTP refresh: " + ", ".join(
                    f"{p['strike']}{p['opt']}={p['ltp']}" for p in self.paper.positions))
            return n > 0
        except Exception as e:
            self.log(f"LTP refresh failed, last prints kept: {str(e)[:100]}")
            for p in self.paper.positions:
                p["live_ltp"] = False
            return False

    def _broker_sym_for(self, pos, expiry_days=None):
        """Resolve Kotak trading symbol for a paper leg. Stored broker_sym wins."""
        if pos.get("broker_sym"):
            return pos["broker_sym"]
        try:
            from datetime import datetime as _dt
            exp = _dt.strptime(pos["expiry"], "%Y-%m-%d").date()
            return self.kotak.find_option_symbol(pos["strike"], pos["opt"], exp)
        except Exception:
            return None

    def _live_cover_all(self, cfg, shut_positions, why):
        """Buy back every leg with limit covers using real broker symbols, then cancel SLs."""
        if not (self.kotak and self.kotak.logged_in):
            self.log(f"live {why}: not logged in, covers NOT placed - square off manually in Neo")
            return
        prod = cfg.get("PRODUCT", "MIS")
        for s in shut_positions or []:
            if not s:
                continue
            try:
                sym = self._broker_sym_for(s)
                if not sym or " " in str(sym):
                    self.log(f"live {why}: BAD symbol for {s.get('symbol')} - cover MANUALLY in Neo")
                    # Re-add to paper tracking so it's not lost
                    self.paper.positions.append(s)
                    continue
                lim = round(float(s.get("exit", s.get("ltp", 0))) * 1.02 + 0.5, 1)
                self.kotak.place_cover(sym, abs(s["qty"]), prod, limit_price=lim)
                self.log(f"live {why}: cover BUY {sym} qty {abs(s['qty'])} L {lim}")
            except Exception as e:
                self.log(f"live {why} cover failed for {s.get('symbol')}: {e} - do it manually")
                # Re-add to paper tracking so it's not lost
                self.paper.positions.append(s)
        try:
            self.log(f"live {why}: SL cancels {self.kotak.cancel_all_sls()}")
        except Exception as e:
            self.log(f"live {why} SL cancel failed: {e}")

    def _extract_order_id(self, res):
        if not isinstance(res, dict):
            return None
        for key in ("orderId", "order_id", "orderid", "data"):
            v = res.get(key)
            if v:
                if isinstance(v, dict):
                    return v.get("orderId") or v.get("order_id") or str(v)
                return str(v)
        return None

    def _enter_legs(self, signal, spot, cfg):
        mode = str(cfg.get("MODE","paper")).lower()
        prem1 = as_float(cfg.get("SHORT_PREMIUM_1",50),50)
        prem2 = as_float(cfg.get("SHORT_PREMIUM_2",30),30)
        wd = as_int(cfg.get("EXPIRY_WEEKDAY",1),1)
        nxt = as_bool(cfg.get("USE_NEXT_WEEKLY",True))
        # Live NSE calendar first (holiday-adjusted: 19-Oct, 27-Oct...).
        # Weekday math fallback only if chain unreachable.
        try:
            from option_chain import resolve_live_expiry
            expiry = resolve_live_expiry(nxt) or resolve_expiry(date.today(), wd, nxt)
        except Exception:
            expiry = resolve_expiry(date.today(), wd, nxt)
        opt = "PE" if signal == "LONG" else "CE"  # long->sell PUT, short->sell CALL
        # LIVE-ONLY entries: strikes + premiums from NSE chain prints. No model.
        # If chain blocked or no strikes <= target, no entry this signal.
        try:
            from option_chain import fetch_chain_v3, pick_strikes
            ch, _ = fetch_chain_v3("NIFTY", expiry)
            picks50 = pick_strikes(ch, prem1)
            picks30 = pick_strikes(ch, prem2)
            # picks30 CE/PE are also <=30 (subset); use per-target picks
            c1 = picks50["CE"] if opt == "CE" else picks50["PE"]
            c2 = picks30["CE"] if opt == "CE" else picks30["PE"]
            if not (c1 and c2):
                self.log(f"chain {expiry}: no strikes <={prem1}/{prem2}, entry skipped")
                return
            (k1, px1), (k2, px2) = c1, c2
        except Exception as e:
            self.log(f"chain fetch failed, entry skipped: {str(e)[:120]}")
            return
        p1 = self.paper.open_short(opt, k1, expiry, px1, spot, "LEG1")
        p2 = self.paper.open_short(opt, k2, expiry, px2, spot, "LEG2")
        self._last_ltp = time.time()  # entry prints are fresh
        with self.lock:
            self.state["ltp_time"] = datetime.now().strftime("%H:%M:%S")
        self.log(f"{signal}: SHORT {opt} {k1}@{px1} / {k2}@{px2} "
                 f"(target <={prem1}/{prem2}) spot {spot:.0f} expiry {expiry} [LIVE-CHAIN]")
        with self.lock:
            self.state["expiry"] = str(expiry)
        if mode == "live":
            if self.kotak and self.kotak.logged_in:
                try:
                    from config import as_float as _f
                    qty = self.paper.lot_qty * self.paper.lots
                    prod = cfg.get("PRODUCT", "MIS")
                    sl1 = _f(cfg.get("SL_POINTS_1", 15), 15)
                    sl2 = _f(cfg.get("SL_PCT_2", 25), 25)
                    live_legs = []  # paper legs with live orders successfully placed
                    for paper_leg, kk, pxx, sl_pts, sl_pct in [
                            (p1, k1, px1, sl1, 0), (p2, k2, px2, 0, sl2)]:
                        sym = self.kotak.find_option_symbol(kk, opt, expiry)
                        if not sym:
                            self.log(f"WARNING: no broker symbol for {kk}{opt} - removing phantom paper leg")
                            self.paper.close(paper_leg["id"], reason="no-broker-symbol")
                            continue
                        if " " in str(sym):
                            self.log(f"WARNING scrip search fell back to '{sym}' - "
                                     f"live SELL skipped for {kk}{opt}, removing phantom leg")
                            self.paper.close(paper_leg["id"], reason="bad-symbol")
                            continue
                        paper_leg["broker_sym"] = sym
                        # limit sell at LTP minus 0.5 concession for instant fill
                        sell_res = self.kotak.place_short(sym, qty, prod,
                                                          limit_price=max(round(pxx - 0.5, 1), 0.5))
                        sell_oid = self._extract_order_id(sell_res)
                        if sell_oid is None:
                            self.log(f"LIVE SELL {sym} rejected by broker ({sell_res}) - removing phantom leg")
                            self.paper.close(paper_leg["id"], reason="broker-reject")
                            continue
                        # broker-native SL buy the moment entry fills
                        sl_res = self.kotak.place_sl_for_short(sym, qty, pxx, prod,
                                                               sl_points=sl_pts or None,
                                                               sl_pct=sl_pct or None)
                        sl_oid = self._extract_order_id(sl_res.get("resp") if isinstance(sl_res, dict) else None)
                        paper_leg["broker_order_id"] = sell_oid
                        paper_leg["sl_order_id"] = sl_oid
                        if sl_oid:
                            self.kotak.sl_orders[sym] = sl_oid
                        live_legs.append(paper_leg)
                        self.log(f"LIVE SELL {sym} qty {qty} @ {pxx-0.5:.1f} order_id={sell_oid} "
                                 f"| SL trigger={sl_res['trigger']} limit={sl_res['limit']} order_id={sl_oid}")
                    # CRITICAL: if one leg succeeded and other failed, cover the successful leg
                    # to avoid an open one-sided short at the broker
                    if len(live_legs) == 1:
                        self.log("LIVE: only one leg placed - covering the successful leg to avoid one-sided short")
                        shut = [live_legs[0]]
                        self.paper.close(live_legs[0]["id"], reason="leg-failed")
                        self.record_closed(shut, "leg-failed")
                        self._live_cover_all(cfg, shut, "single-leg-fail")
                    elif len(live_legs) == 0:
                        self.log("LIVE: both legs failed - no positions opened")
                except Exception as e:
                    self.log(f"live sell failed: {e} - removing all paper legs for this entry")
                    for p in [p1, p2]:
                        try:
                            self.paper.close(p["id"], reason="live-error")
                        except Exception:
                            pass
            else:
                self.log("live mode but Kotak not logged in - paper legs only, login first")

    def _maybe_telegram(self, cfg, force=False, note=""):
        now = time.time()
        if force or (now - self._last_tg >= 15*60):
            self._last_tg = now
            with self.lock:
                snap = dict(self.state)
                snap["note"] = note or snap.get("note","")
            txt = positions_text(snap)
            res = send_telegram(cfg.get("TELEGRAM_BOT_TOKEN",""),
                                cfg.get("TELEGRAM_CHAT_ID",""), txt)
            with self.lock:
                self.state["last_telegram"] = datetime.now().strftime("%H:%M:%S") + (" OK" if res.get("ok") else f" ({res.get('error','fail')})")
            if res.get("ok"):
                self.log("telegram 15-min update sent")

    def _telegram(self, cfg, force=False, note=""):
        return self._maybe_telegram(cfg, force=force, note=note)


ENGINE = Engine()
