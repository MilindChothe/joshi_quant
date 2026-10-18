"""Paper broker (simulation) + Kotak Neo live broker with same interface."""
import time
from datetime import datetime


class PaperBroker:
    """Tracks short PUT/CALL legs. LTPs are NSE live-chain prints only - no model."""

    def __init__(self, lot_qty=75, lots=1):
        self.lot_qty = lot_qty
        self.lots = lots
        self.positions = []  # list of dicts
        self.realized = 0.0

    def open_short(self, opt, strike, expiry, premium, spot, tag):
        qty = -self.lot_qty * self.lots  # short
        pid = f"{tag}-{opt}-{strike}-{len(self.positions)}-{int(time.time())}"
        pos = {
            "id": pid, "symbol": f"NIFTY {expiry} {strike}{opt}",
            "opt": opt, "strike": strike, "expiry": str(expiry),
            "side": "SHORT", "qty": qty, "entry": float(premium),
            "ltp": float(premium), "pnl": 0.0, "tag": tag,
            "time": datetime.now().strftime("%H:%M:%S"),
            "entry_time": datetime.now().strftime("%H:%M:%S"),
            "live_ltp": True,  # entry print is live
        }
        self.positions.append(pos)
        return pos

    def update_mtm(self, spot=None):
        # Recompute P/L from stored (live) LTPs only. Live refresh happens in engine.
        mtm = self.realized
        for p in self.positions:
            p["pnl"] = round((p["entry"] - p["ltp"]) * abs(p["qty"]), 2)
            mtm += p["pnl"]
        return mtm

    def close(self, pid, ltp=None, reason=""):
        for i, p in enumerate(self.positions):
            if p["id"] == pid:
                from datetime import datetime as _dt
                exit_px = ltp if ltp is not None else p["ltp"]
                pnl = (p["entry"] - exit_px) * abs(p["qty"])
                self.realized += pnl
                shut = self.positions.pop(i) | {"exit": round(exit_px, 2),
                                                "exit_time": _dt.now().strftime("%H:%M:%S"),
                                                "realized": round(pnl, 2),
                                                "exit_reason": reason}
                return shut
        return None

    def close_all(self):
        closed = []
        for p in list(self.positions):
            closed.append(self.close(p["id"]))
        return closed

    def check_sl(self, pos, sl_points=None, sl_pct=None):
        """True if SL breached for a SHORT leg."""
        if sl_points:
            if pos["ltp"] >= pos["entry"] + sl_points:
                return True
        if sl_pct:
            if pos["ltp"] >= pos["entry"] * (1 + sl_pct / 100.0):
                return True
        return False


class KotakBroker:
    """Thin wrapper over kotak-neo-api-v2. Falls back gracefully when not logged in."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        self.logged_in = False
        self.login_error = ""
        self.sl_orders = {}  # trading_symbol -> order_id
        self.paper = PaperBroker(
            lot_qty=int(float(cfg.get("NIFTY_LOT_QTY", 75))),
            lots=int(float(cfg.get("LOTS", 1))),
        )

    def login(self, totp_code: str = ""):
        try:
            from neo_api_client import NeoAPI
        except ImportError as e:
            self.login_error = "kotakneoapi not installed. pip install git+https://github.com/Kotak-Neo/kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
            return False, self.login_error
        try:
            import pyotp
            ck = self.cfg.get("KOTAK_CONSUMER_KEY", "")
            mob = self.cfg.get("KOTAK_MOBILE_NUMBER", "")
            ucc = self.cfg.get("KOTAK_UCC", "")
            mpin = self.cfg.get("KOTAK_MPIN", "")
            secret = self.cfg.get("KOTAK_TOTP_SECRET", "")
            if not (ck and mob and ucc and mpin):
                raise RuntimeError("Fill ConsumerKey / Mobile / UCC / MPIN on web page first.")
            if not totp_code and secret:
                totp_code = pyotp.TOTP(secret).now()
            if not totp_code:
                raise RuntimeError("TOTP required: enter current authenticator code on web page.")
            self.client = NeoAPI(environment=self.cfg.get("KOTAK_ENVIRONMENT", "prod"),
                                 consumer_key=ck)
            self.client.totp_login(mobile_number=mob, ucc=ucc, totp=totp_code)
            self.client.totp_validate(mpin=mpin)
            self.logged_in = True
            return True, "Kotak login OK"
        except Exception as e:
            self.login_error = str(e)
            self.logged_in = False
            return False, self.login_error

    # ---- live helpers (best-effort; paper-simulated MTM still shown) ----
    def find_option_symbol(self, strike, opt, expiry_date):
        """Search scrip master for NIFTY option trading symbol. Returns string symbol or None."""
        try:
            exp_fmt = expiry_date.strftime("%d%b%y").upper()
            res = self.client.search_scrip(exchange_segment="nse_fo", symbol="NIFTY",
                                           expiry=exp_fmt, option_type="CE" if opt == "CE" else "PE",
                                           strike_price=str(strike))
            data = res if isinstance(res, list) else res.get("data", res) if isinstance(res, dict) else []
            if data:
                row = data[0]
                ts = row.get("trading_symbol") or row.get("pSymbol") or row.get("symbol") or row.get("tsym")
                if ts is not None:
                    ts = str(ts).strip()
                    # numeric value is an instrument token, not a real symbol string
                    if not ts.isdigit():
                        return ts
                # fallback symbol string in Kotak Neo format
                return f"NIFTY {exp_fmt} {strike}{opt}"
        except Exception:
            pass
        return None

    def place_short(self, trading_symbol, qty, product="MIS", limit_price=None):
        """Limit sell (Kotak blocks MKT on stock options). limit defaults to 0 (rejected) - always pass LTP."""
        return self.client.place_order(
            exchange_segment="nse_fo", product=product, price=str(limit_price or 0),
            order_type="L", quantity=str(abs(qty)), validity="DAY",
            trading_symbol=trading_symbol, transaction_type="S",
            amo="NO", disclosed_quantity="0", market_protection="0", pf="N",
            trigger_price="0", tag="20SMA",
        )

    def place_cover(self, trading_symbol, qty, product="MIS", limit_price=None):
        """Limit buy to cover. Buffer handled by caller."""
        return self.client.place_order(
            exchange_segment="nse_fo", product=product, price=str(limit_price or 0),
            order_type="L", quantity=str(abs(qty)), validity="DAY",
            trading_symbol=trading_symbol, transaction_type="B",
            amo="NO", disclosed_quantity="0", market_protection="0", pf="N",
            trigger_price="0", tag="20SMA-EXIT",
        )

    def place_sl_for_short(self, trading_symbol, qty, entry_px, product="MIS",
                           sl_points=None, sl_pct=None):
        """Native broker SL-BUY resting in open orders from entry time.
        trigger = entry + points (LEG1) or entry*(1+pct) (LEG2). Limit = trigger + buffer."""
        entry = float(entry_px)
        if sl_points:
            trigger = entry + float(sl_points)
        elif sl_pct:
            trigger = entry * (1 + float(sl_pct) / 100.0)
        else:
            raise ValueError("sl_points or sl_pct required")
        trigger = round(trigger, 1)
        limit = round(trigger * 1.02 + 0.5, 1)  # buffer so SL-L doesn't miss in spike
        res = self.client.place_order(
            exchange_segment="nse_fo", product=product, price=str(limit),
            order_type="SL", quantity=str(abs(qty)), validity="DAY",
            trading_symbol=trading_symbol, transaction_type="B",
            amo="NO", disclosed_quantity="0", market_protection="0", pf="N",
            trigger_price=str(trigger), tag="20SMA-SL",
        )
        try:
            oid = res.get("orderId") if isinstance(res, dict) else None
            if oid:
                self.sl_orders[trading_symbol] = oid
        except Exception:
            pass
        return {"trigger": trigger, "limit": limit, "resp": res}

    def cancel_sl(self, trading_symbol):
        oid = self.sl_orders.pop(trading_symbol, None)
        if not oid:
            return {"skipped": "no SL tracked"}
        try:
            return self.client.cancel_order(order_id=str(oid))
        except Exception as e:
            return {"error": str(e)}

    def cancel_all_sls(self):
        out = {}
        for sym, oid in list(self.sl_orders.items()):
            try:
                out[sym] = self.client.cancel_order(order_id=str(oid))
            except Exception as e:
                out[sym] = str(e)
            self.sl_orders.pop(sym, None)
        return out
