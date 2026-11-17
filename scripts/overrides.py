"""Hand-kept, sourced fixes for the market and company builds (build_data.py, build_companies.py).

share_overrides.json   share count (and optional net debt) for a new listing that Yahoo does not cover yet.
                       Used only while Yahoo has no share count / market cap / balance sheet for the symbol, so
                       the override drops out by itself once Yahoo catches up (the build logs that it is stale).
price_adjustments.json back-adjustment of daily history for a spin-out or other capital event Yahoo has not
                       adjusted. Applied only while Yahoo's close on the day before the ex-date still equals the
                       recorded unadjusted close, so it never double-adjusts once Yahoo fixes its history.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return (json.load(f) or {}).get("symbols") or {}
    except FileNotFoundError:
        return {}


SHARES = _load("share_overrides.json")
ADJUST = _load("price_adjustments.json")


def share_override(sym):
    """-> override dict (shares, net_debt, ccy, source, as_of) or None."""
    return SHARES.get(sym)


def fill_cap(sym, mcap, px, ccy, shares_yahoo=None, log=print):
    """Market cap from the override share count when Yahoo has neither a cap nor a share count. -> (mcap, used?)"""
    o = SHARES.get(sym)
    if not o:
        return mcap, False
    if mcap is not None or shares_yahoo:
        log(f"  {sym}: Yahoo now has a market cap / share count; share override in share_overrides.json is no longer used")
        return mcap, False
    if not px:
        return mcap, False
    minor = 100.0 if ccy in ("GBp", "GBX", "ILA", "ZAc") else 1.0
    return float(o["shares"]) * float(px) / minor, True


def fill_balance(sym, bal):
    """Net debt from the override when Yahoo has no balance sheet. -> (bal, used?)"""
    o = SHARES.get(sym)
    if not o or o.get("net_debt") is None or (bal and bal.get("nd") is not None):
        return bal, False
    return dict(nd=float(o["net_debt"]), mi=None, asof=o.get("as_of")), True


def fill_profile(sym, detail):
    """Fill an empty Yahoo profile (about, officers) from share_overrides.json; Yahoo's wins once it has one."""
    pr = (share_override(sym) or {}).get("profile")
    if not pr:
        return False
    used = False
    a = detail.setdefault("about", {}) or {}
    if not a.get("summary"):
        a.update({k: pr.get(k) for k in ("summary", "web", "city", "state", "country") if pr.get(k)})
        a["lead"] = a.get("lead") or pr["summary"].split(". ")[0] + "."
        a["src"] = pr.get("source")
        detail["about"] = a
        used = True
    if not detail.get("officers") and pr.get("officers"):
        detail["officers"] = [dict(o) for o in pr["officers"]]
        detail["officers_src"] = pr.get("source")
        used = True
    return used


def adjust_history(sym, df, log=print):
    """Back-adjust open/high/low/close before each ex_date by its factor (volume unchanged). -> (df, applied list)."""
    events = ADJUST.get(sym) or []
    if not events or df is None or df.empty:
        return df, []
    dates = df.index.strftime("%Y-%m-%d")
    applied = []
    df = df.copy()
    for ev in events:
        before = dates < ev["ex_date"]
        if not before.any():
            continue
        raw_prev = df["close"][before].dropna()
        if raw_prev.empty:
            continue
        if abs(float(raw_prev.iloc[-1]) - float(ev["prev_close"])) > 0.005 * float(ev["prev_close"]):
            log(f"  {sym}: {ev['ex_date']} adjustment skipped (Yahoo history already adjusted or changed)")
            continue
        for col in ("open", "high", "low", "close"):
            if col in df:
                df.loc[before, col] = df.loc[before, col] * float(ev["factor"])
        applied.append(ev["ex_date"])
        log(f"  {sym}: history before {ev['ex_date']} x {ev['factor']} ({ev.get('reason', '')})")
    return df, applied
