#!/usr/bin/env python3
"""Build data/symbols.json — client-side Markets chart search index.

TradingView's symbol-search API returns 403 from static hosts, so the Monitor
search box needs a local catalog. This script builds a few-thousand-row index
covering London (LSE/AIM), Sweden (OMX Stockholm), Norway (Oslo), New York
(NYSE/Nasdaq/Arca), Toronto (TSX/TSXV) and Hong Kong (HKEX) without API keys.

Sources (no keys):
  - Nasdaq Trader symbol directories (US common stock + ETF)
  - adanos-software/free-ticker-database CSV (filtered multi-exchange)
  - scripts/universe.py TradingView mappings
  - curated energy / mining / renewables peers (must-include)

Exotic names may still need EXCH:SYM or the in-widget TradingView search.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from universe import GROUPS  # noqa: E402

UA = {"User-Agent": "markets-monitor-symbol-builder/1.0"}
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
FREE_TICKERS = (
    "https://raw.githubusercontent.com/adanos-software/free-ticker-database/"
    "main/data/tickers.csv"
)

# free-ticker-database exchange → TradingView prefix
# Scope: London LSE/AIM, Sweden OMXSTO, Norway OSL, NYSE/Nasdaq (+ NYSE Arca ETFs),
# Toronto TSX/TSXV, Hong Kong HKEX. Other markets are omitted to keep the file small.
EX_MAP = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE ARCA": "AMEX",
    "LSE": "LSE",
    "OSL": "OSL",
    "STO": "OMXSTO",
    "TSX": "TSX",
    "TSXV": "TSXV",
    "HKEX": "HKEX",
}

NOISE_RE = re.compile(
    r"\b(warrant|warrants|unit|units|right|rights|preferred|preference|"
    r"note|notes|bond|debenture|test issue|when issued)\b",
    re.I,
)
# LSE International Order Book dual-listings (e.g. 0N9S)
LSE_IOB_RE = re.compile(r"^0[A-Z0-9]{3}$")
NAME_TRIM_RE = re.compile(
    r"\s*[-–—,]?\s*(Common Stock|Ordinary Shares?|Ordinary Share|"
    r"Class [A-Z](?:\s+Ordinary Shares?)?|American Depositary Shares?|"
    r"ADS|ADR|GDR|Registered Shares?|Public Limited Company|"
    r"\(publ\)|\(each representing[^)]*\))\s*$",
    re.I,
)
KIND_MAP = {"equity": "Stock", "etf": "ETF", "index": "Index", "Stock": "Stock", "ETF": "ETF"}


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_name(name: str) -> str:
    n = (name or "").strip()
    # repeatedly strip trailing legal/share-class suffixes
    for _ in range(4):
        n2 = NAME_TRIM_RE.sub("", n).strip(" -–,")
        if n2 == n:
            break
        n = n2
    n = re.sub(r"\s+", " ", n).strip()
    return n or name.strip()


def tv_ticker(exchange: str, ticker: str) -> str:
    t = (ticker or "").strip().upper().replace(" ", "")
    if exchange in ("STO", "CPH", "OMXSTO", "OMXCOP"):
        t = t.replace("-", "_")
    if exchange == "HKEX" and t.isdigit():
        t = str(int(t))  # 0700 → 700
    # Nasdaq class shares use BRK.A; keep as-is
    return t


def add(rows: dict[str, tuple[str, str, str]], name: str, symbol: str, kind: str) -> None:
    if not name or not symbol or ":" not in symbol:
        return
    sym = symbol.strip().upper()
    k = KIND_MAP.get(kind, kind or "Stock")
    # prefer earlier / curated entries; only overwrite if new name is longer/cleaner
    if sym in rows:
        return
    rows[sym] = (clean_name(name), sym, k)


# Must-include energy / mining / renewables peers (LSE AIM, Nordics, etc.)
CURATED = [
    # London LSE / AIM — energy, mining, renewables peers
    ("Genel Energy", "LSE:GENL", "Stock"),
    ("Gulf Keystone Petroleum", "LSE:GKP", "Stock"),
    ("Tullow Oil", "LSE:TLW", "Stock"),
    ("Capricorn Energy", "LSE:CNE", "Stock"),
    ("Energean", "LSE:ENOG", "Stock"),
    ("Ithaca Energy", "LSE:ITH", "Stock"),
    ("Harbour Energy", "LSE:HBR", "Stock"),
    ("EnQuest", "LSE:ENQ", "Stock"),
    ("Pantheon Resources", "LSE:PANR", "Stock"),
    ("Rockhopper Exploration", "LSE:RKH", "Stock"),
    ("Pharos Energy", "LSE:PHAR", "Stock"),
    ("Afentra", "LSE:AET", "Stock"),
    ("Seplat Energy", "LSE:SEPL", "Stock"),
    ("Diversified Energy", "LSE:DEC", "Stock"),
    ("Serica Energy", "LSE:SQZ", "Stock"),
    ("Kistos Holdings", "LSE:KIST", "Stock"),
    ("Shell", "LSE:SHEL", "Stock"),
    ("BP", "LSE:BP", "Stock"),
    ("Glencore", "LSE:GLEN", "Stock"),
    ("BHP", "LSE:BHP", "Stock"),
    ("Rio Tinto", "LSE:RIO", "Stock"),
    ("Anglo American", "LSE:AAL", "Stock"),
    ("Antofagasta", "LSE:ANTO", "Stock"),
    ("Fresnillo", "LSE:FRES", "Stock"),
    ("Centamin", "LSE:CEY", "Stock"),
    ("Endeavour Mining", "LSE:EDV", "Stock"),
    ("Atalaya Mining", "LSE:ATYM", "Stock"),
    ("Hochschild Mining", "LSE:HOC", "Stock"),
    ("Yellow Cake", "LSE:YCA", "Stock"),
    ("Drax Group", "LSE:DRX", "Stock"),
    ("SSE", "LSE:SSE", "Stock"),
    ("National Grid", "LSE:NG", "Stock"),
    ("Ceres Power", "LSE:CWR", "Stock"),
    ("ITM Power", "LSE:ITM", "Stock"),
    # Norway
    ("Yara International", "OSL:YAR", "Stock"),
    ("Subsea 7", "OSL:SUBC", "Stock"),
    ("TGS", "OSL:TGS", "Stock"),
    ("PGS", "OSL:PGS", "Stock"),
    ("BW LPG", "OSL:BWLPG", "Stock"),
    ("Frontline", "OSL:FRO", "Stock"),
    ("Golden Ocean", "OSL:GOGL", "Stock"),
    ("DNO", "OSL:DNO", "Stock"),
    ("Panoro Energy", "OSL:PEN", "Stock"),
    ("BlueNord", "OSL:BNOR", "Stock"),
    # Canada
    ("Lundin Mining", "TSX:LUN", "Stock"),
    ("Lundin Gold", "TSX:LUG", "Stock"),
    ("International Petroleum Corp", "TSX:IPCO", "Stock"),
    ("Cenovus Energy", "TSX:CVE", "Stock"),
    ("Suncor Energy", "TSX:SU", "Stock"),
    ("Imperial Oil", "TSX:IMO", "Stock"),
    ("Canadian Natural Resources", "TSX:CNQ", "Stock"),
    ("Tourmaline Oil", "TSX:TOU", "Stock"),
    ("Arc Resources", "TSX:ARX", "Stock"),
    ("MEG Energy", "TSX:MEG", "Stock"),
    ("Whitecap Resources", "TSX:WCP", "Stock"),
    ("Teck Resources B", "TSX:TECK.B", "Stock"),
    ("First Quantum", "TSX:FM", "Stock"),
    ("Ivanhoe Mines", "TSX:IVN", "Stock"),
    ("Kinross Gold", "TSX:K", "Stock"),
    ("Wheaton Precious Metals", "TSX:WPM", "Stock"),
    ("Franco-Nevada", "TSX:FNV", "Stock"),
    # US
    ("Cameco", "NYSE:CCJ", "Stock"),
    ("Occidental Petroleum", "NYSE:OXY", "Stock"),
    ("EOG Resources", "NYSE:EOG", "Stock"),
    ("Devon Energy", "NYSE:DVN", "Stock"),
    ("Hess", "NYSE:HES", "Stock"),
    ("Marathon Petroleum", "NYSE:MPC", "Stock"),
    ("Valero Energy", "NYSE:VLO", "Stock"),
    ("Schlumberger", "NYSE:SLB", "Stock"),
    ("Halliburton", "NYSE:HAL", "Stock"),
    ("Baker Hughes", "NASDAQ:BKR", "Stock"),
    ("Chevron", "NYSE:CVX", "Stock"),
    ("Exxon Mobil", "NYSE:XOM", "Stock"),
    ("ConocoPhillips", "NYSE:COP", "Stock"),
    ("Diamondback Energy", "NASDAQ:FANG", "Stock"),
    # Hong Kong
    ("Tencent", "HKEX:700", "Stock"),
    ("China Mobile", "HKEX:941", "Stock"),
    ("CNOOC", "HKEX:883", "Stock"),
    ("PetroChina", "HKEX:857", "Stock"),
    ("Sinopec", "HKEX:386", "Stock"),
    ("Alibaba", "HKEX:9988", "Stock"),
]

def from_universe(rows: dict) -> None:
    for g in GROUPS:
        for sec in g["sections"]:
            for r in sec["rows"]:
                tv = r.get("tv")
                if not tv:
                    continue
                kind = r.get("kind") or "equity"
                if kind in ("future", "fx", "yield"):
                    continue
                add(rows, r["name"], tv, kind)


def from_curated(rows: dict) -> None:
    for name, sym, kind in CURATED:
        add(rows, name, sym, kind)


def from_nasdaq(rows: dict) -> tuple[int, int]:
    n_ok = n_skip = 0
    # nasdaqlisted
    text = fetch(NASDAQ_LISTED).decode("latin-1")
    for line in text.splitlines():
        if not line or line.startswith("Symbol|") or line.startswith("File Creation"):
            continue
        p = line.split("|")
        if len(p) < 7:
            continue
        sym, name, _cat, test, _fin, _lot, etf = p[:7]
        if test == "Y":
            n_skip += 1
            continue
        if NOISE_RE.search(name):
            n_skip += 1
            continue
        # skip warrants/units by ticker suffix
        if any(sym.endswith(x) for x in ("W", "U", "R", "WS", "WT")) and " " not in sym:
            # too aggressive for tickers ending in W; only skip if name hints
            pass
        if re.search(r"\b(Warrant|Unit|Right)\b", name, re.I):
            n_skip += 1
            continue
        kind = "ETF" if etf == "Y" else "Stock"
        add(rows, name, f"NASDAQ:{sym}", kind)
        n_ok += 1

    # otherlisted (NYSE / NYSE MKT / ARCA / BATS / IEX)
    text = fetch(OTHER_LISTED).decode("latin-1")
    ex_code = {"N": "NYSE", "A": "AMEX", "P": "AMEX", "Z": "BATS", "V": "IEX"}
    for line in text.splitlines():
        if not line or line.startswith("ACT Symbol|") or line.startswith("File Creation"):
            continue
        p = line.split("|")
        if len(p) < 7:
            continue
        sym, name, exch, _cqs, etf, _lot, test = p[:7]
        if test == "Y":
            n_skip += 1
            continue
        if NOISE_RE.search(name):
            n_skip += 1
            continue
        prefix = ex_code.get(exch)
        if not prefix or prefix in ("BATS", "IEX"):
            # BATS/IEX often duplicate ARCA/NYSE listings; skip to shrink
            n_skip += 1
            continue
        kind = "ETF" if etf == "Y" else "Stock"
        add(rows, name, f"{prefix}:{sym}", kind)
        n_ok += 1
    return n_ok, n_skip


def from_free_tickers(rows: dict) -> tuple[int, int]:
    raw = fetch(FREE_TICKERS)
    n_ok = n_skip = 0
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    for row in reader:
        ex = row.get("exchange") or ""
        if ex not in EX_MAP:
            n_skip += 1
            continue
        asset = row.get("asset_type") or ""
        if asset not in ("Stock", "ETF"):
            n_skip += 1
            continue
        # US ETFs already covered by Nasdaq dirs; keep non-US stocks primarily
        if asset == "ETF" and ex not in ("SIX",):
            # allow a few major non-US later via curated/universe; skip flood
            if ex not in ("NASDAQ", "NYSE", "NYSE ARCA"):
                n_skip += 1
                continue
        name = row.get("name") or ""
        ticker = (row.get("ticker") or "").strip()
        if not ticker or not name:
            n_skip += 1
            continue
        if NOISE_RE.search(name):
            n_skip += 1
            continue
        if ex == "LSE" and LSE_IOB_RE.match(ticker.upper()):
            n_skip += 1
            continue
        if re.search(r"\b(GDR|ADR)\b", name) and ex == "LSE":
            n_skip += 1
            continue
        # skip US rows here — Nasdaq dirs are cleaner / official
        if ex in ("NASDAQ", "NYSE", "NYSE ARCA"):
            n_skip += 1
            continue
        prefix = EX_MAP[ex]
        t = tv_ticker(ex, ticker)
        if not t:
            n_skip += 1
            continue
        add(rows, name, f"{prefix}:{t}", asset)
        n_ok += 1
    return n_ok, n_skip


def build(out: Path) -> dict:
    rows: dict[str, tuple[str, str, str]] = {}
    # curated + universe first so they win on collisions
    from_curated(rows)
    from_universe(rows)
    log(f"seed curated+universe: {len(rows)}")

    try:
        ok, skip = from_nasdaq(rows)
        log(f"nasdaq directories: +{ok} kept-attempt, {skip} skipped, total {len(rows)}")
    except Exception as e:
        log(f"WARNING nasdaq directories failed: {e}")

    try:
        ok, skip = from_free_tickers(rows)
        log(f"free-ticker-db: +{ok} kept-attempt, {skip} skipped, total {len(rows)}")
    except Exception as e:
        log(f"WARNING free-ticker-db failed: {e}")

    symbols = sorted(rows.values(), key=lambda x: (x[1], x[0]))
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": (
            "Nasdaq Trader + free-ticker-database (LSE/AIM, OMXSTO, OSL, NYSE/Nasdaq/Arca, "
            "TSX/TSXV, HKEX) + universe.py TV symbols + curated energy/mining peers"
        ),
        "count": len(symbols),
        "note": (
            "Client-side Markets chart autofill. TradingView symbol-search is often "
            "blocked (403) from static hosts. Exotic names may need EXCH:SYM or the "
            "in-widget TradingView search."
        ),
        "symbols": [[n, s, k] for n, s, k in symbols],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {out} ({out.stat().st_size} bytes, {len(symbols)} symbols)")
    # sanity: Genel must be present
    genel = [x for x in symbols if x[1] == "LSE:GENL" or "genel" in x[0].lower()]
    log(f"Genel check: {genel[:3]}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "symbols.json",
        help="output path (default: data/symbols.json)",
    )
    args = ap.parse_args()
    t0 = time.time()
    build(args.out)
    log(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
