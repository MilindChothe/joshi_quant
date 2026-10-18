"""Live NSE option-chain fetch (v3, Oct-2026) + <=target strike picker."""
import requests
from datetime import date

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE, timeout=15)
    return s


def contract_info(symbol="NIFTY", session=None):
    s = session or _session()
    r = s.get(f"{BASE}/api/option-chain-contract-info",
              params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    return r.json(), s


def fetch_chain_v3(symbol="NIFTY", expiry: date = None, session=None):
    """expiry as date -> '27-Oct-2026'. Returns (json, session)."""
    s = session or _session()
    params = {"type": "Indices", "symbol": symbol}
    if expiry:
        params["expiry"] = expiry.strftime("%d-%b-%Y")
    else:
        info, s = contract_info(symbol, s)
        exps = info.get("expiryDates", [])
        if exps:
            params["expiry"] = exps[0]
    r = s.get(f"{BASE}/api/option-chain-v3", params=params, timeout=15)
    r.raise_for_status()
    return r.json(), s


def _rows(chain):
    # v3 shape: records.data[] with CE/PE; keep compat with old shape
    if isinstance(chain, dict):
        if "records" in chain and isinstance(chain["records"], dict):
            d = chain["records"].get("data")
            if d:
                return d
        for k in ("data", "filteredData", "opChains"):
            if isinstance(chain.get(k), list):
                return chain[k]
    return []


def spot_from_chain(chain):
    try:
        return float(chain["records"]["underlyingValue"])
    except Exception:
        pass
    try:
        return float(chain.get("underlyingValue", 0))
    except Exception:
        return 0.0


def resolve_live_expiry(use_next_weekly: bool = True, today: date = None):
    """Pick from NSE's real expiry calendar (holiday-adjusted).
    First expiry >= today = current weekly, second = next weekly.
    Today 14-Oct -> current 19-Oct, next 27-Oct."""
    from datetime import datetime as _dt
    today = today or date.today()
    info, _ = contract_info("NIFTY")
    dates = []
    for e in info.get("expiryDates", []):
        try:
            d = _dt.strptime(e.strip(), "%d-%b-%Y").date()
        except Exception:
            continue
        if d >= today:
            dates.append((d, e))
    dates.sort()
    if not dates:
        return None
    if use_next_weekly:
        return dates[1][0] if len(dates) > 1 else dates[0][0]
    return dates[0][0]


def ltps_for(chain):
    """{(strike, 'CE'/'PE'): lastPrice} for open-position MTM refresh."""
    out = {}
    for d in _rows(chain):
        st = d.get("strikePrice")
        for k in ("CE", "PE"):
            px = (d.get(k) or {}).get("lastPrice")
            if isinstance(px, (int, float)) and px > 0:
                out[(st, k)] = float(px)
    return out


def pick_strikes(chain, target: float = 50.0):
    """Highest premium <= target per side from already-expiry-filtered v3 data."""
    ce_cand, pe_cand = [], []
    for d in _rows(chain):
        st = d.get("strikePrice")
        ce = (d.get("CE") or {}).get("lastPrice")
        pe = (d.get("PE") or {}).get("lastPrice")
        if isinstance(ce, (int, float)) and 0 < ce <= target:
            ce_cand.append((st, float(ce)))
        if isinstance(pe, (int, float)) and 0 < pe <= target:
            pe_cand.append((st, float(pe)))
    ce_pick = max(ce_cand, key=lambda x: x[1]) if ce_cand else None
    pe_pick = max(pe_cand, key=lambda x: x[1]) if pe_cand else None
    return {"CE": ce_pick, "PE": pe_pick,
            "ce_count": len(ce_cand), "pe_count": len(pe_cand)}
