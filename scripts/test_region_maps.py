"""Regional sector maps (scripts/build_region_maps.py): session modes and labels on the Zurich clock, holiday labels,
sizing per index (USD cap, EUR cap via FX, CSI weight), stale fallback, and the shipped constituents. No network."""
import datetime as dt
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_region_maps as M  # noqa: E402
import universe  # noqa: E402

ZRH = ZoneInfo("Europe/Zurich")
U = M.load_universe()
C = M.Clock(U["holidays"])
z = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=ZRH)
d = dt.date.fromisoformat

# constituents: the four Pulse v1.10 universes
assert {k: len(v) for k, v in U["maps"].items()} == {"sp500": 500, "eu80": 80, "nikkei225": 225, "csi300": 300}, "four universes"
assert all(len(r) == 6 and r[3] for v in U["maps"].values() for r in v), "every row: yahoo, label, name, sector, size, ccy"
assert "2026-11-23" in U["holidays"]["JPX"] and "2026-11-26" in U["holidays"]["NYSE"], "Tokyo and NYSE holidays shipped"

# session states and labels
assert C.state("nyse", z("2026-11-23T16:05")) == "live"
assert C.state("nyse", z("2026-11-26T16:05")) == "holiday", "Thanksgiving"
assert C.state("tse", z("2026-11-24T04:10")) == "midday", "Tokyo lunch"
assert C.state("sse", z("2026-11-24T01:00")) == "preopen"
assert C.state("xetra", z("2026-11-28T12:00")) == "weekend"
assert C.mode("nyse", d("2026-11-23"), z("2026-11-23T16:05")) == ("live", d("2026-11-23"))
assert C.mode("nyse", d("2026-11-20"), z("2026-11-23T16:05")) == ("close", d("2026-11-20")), "open but no bar today: last close, never live"
assert C.mode("nyse", d("2026-11-23"), z("2026-11-23T23:00")) == ("close", d("2026-11-23"))
assert C.mode("tse", d("2026-11-24"), z("2026-11-24T00:30")) == ("close", None), "stray pre-open bar: use the bar before"
assert C.label("nyse", "live", d("2026-11-23"), z("2026-11-23T16:05")) == "Live 16:05 CET"
assert C.label("tse", "midday", d("2026-11-24"), z("2026-11-24T04:10")) == "Midday 04:10 CET"
assert C.label("nyse", "close", d("2026-11-20"), z("2026-11-23T08:00")) == "Close Fri 20 Nov"
assert C.label("tse", "close", d("2026-11-20"), z("2026-11-23T05:00")) == "Close Fri 20 Nov \u00b7 Tokyo shut", "Labour Thanksgiving Day"
assert C.label("nyse", "close", d("2026-11-25"), z("2026-11-26T16:00")) == "Close Wed 25 Nov \u00b7 New York shut"

# one map from synthetic bars
D0, D1 = d("2026-11-20"), d("2026-11-23")
rows = [["AAA", "AAA", "A Inc", "Information Technology", 100, "USD"], ["BBB", "BBB", "B Inc", "Energy", 300, "USD"],
        ["CCC", "CCC", "C Inc", "Energy", 50, "USD"], ["DDD", "DDD", "D Inc", "Energy", 50, "USD"], ["EEE", "EEE", "E", "Energy", 10, "USD"],
        ["MISS", "MISS", "Missing", "Energy", 10, "USD"]]
px = {"^GSPC": [(D0, 100.0), (D1, 101.0)], "AAA": [(D0, 10.0), (D1, 11.0)], "BBB": [(D0, 10.0), (D1, 9.0)],
      "CCC": [(D0, 5.0), (D1, 5.0)], "DDD": [(D0, 5.0), (D1, 5.5)], "EEE": [(D0, 2.0), (D1, 2.0)]}
m = M.build_map("sp500", rows, px, {"AAA": {"longName": "Alpha Corp"}}, C, z("2026-11-23T23:30"), U["sector_short"])
assert m["mode"] == "close" and m["label"] == "Close Mon 23 Nov" and m["basis"] == "20 Nov \u2192 23 Nov", m["label"]
assert (m["n"], m["N"], m["adv"], m["dec"], m["unch"]) == (5, 6, 2, 1, 2) and m["missing"] == ["MISS"]
assert m["index_chg"] == 1.0
it = {i[0]: i for s in m["sectors"] for i in s["items"]}
assert it["AAA"][2] == "Alpha Corp" and it["AAA"][3] == 10.0 and it["BBB"][3] == -10.0, "Yahoo long name, % move"
assert abs(sum(s["wt"] for s in m["sectors"]) - 100) < 0.2 and m["sectors"][0]["short"] == "Energy", "weights add to 100, biggest sector first"
assert it["BBB"][6] == round(300 * 9 / 1e9, 2), "cap in bn"
live = M.build_map("sp500", rows, px, {}, C, z("2026-11-23T16:05"), U["sector_short"])
assert live["mode"] == "live" and live["label"] == "Live 16:05 CET" and live["basis"] == "vs previous close"
# Europe in EUR (GBp quotes in pence), CSI by weight
eu = [["X.L", "X", "X plc", "Energy", 100, "GBp"], ["Y.DE", "Y", "Y AG", "Energy", 100, "EUR"]] + [[f"Z{i}.DE", "Z", "Z", "Energy", 1, "EUR"] for i in range(4)]
pxe = {"X.L": [(D0, 1000.0), (D1, 1000.0)], "Y.DE": [(D0, 10.0), (D1, 10.0)], "EURGBP=X": [(D0, 0.8), (D1, 0.8)],
       **{f"Z{i}.DE": [(D0, 1.0), (D1, 1.0)] for i in range(4)}}
me = M.build_map("eu80", eu, pxe, {}, C, z("2026-11-23T23:30"), U["sector_short"])
ie = {i[0]: i for s in me["sectors"] for i in s["items"]}
assert ie["X.L"][6] == round(100 * 1000 / 80 / 1e9, 2), "GBp -> EUR: shares x pence / (100 x EURGBP)"
cs = [[f"{i}.SS", str(i), str(i), "Financials" if i % 2 else "Energy", 1.0 + i, "CNY"] for i in range(6)]
pxc = {f"{i}.SS": [(D0, 10.0), (D1, 11.0 if i == 5 else 10.0)] for i in range(6)}
mc = M.build_map("csi300", cs, pxc, {}, C, z("2026-11-23T23:30"), U["sector_short"], "20261030")
assert mc["size"] == "Index weight (30 Oct weights)" and all(i[6] is None for s in mc["sectors"] for i in s["items"])
try:
    M.build_map("sp500", rows, {}, {}, C, z("2026-11-23T23:30"))
    raise AssertionError("an unpriced map must fail")
except RuntimeError:
    pass

# Yahoo series: a latest session missing from the 5d bars is filled from meta (seen 24 Nov 2026 00:20 CET)
import datetime as _dt
ts = lambda iso: int(_dt.datetime.fromisoformat(iso).timestamp())
res = {"meta": {"exchangeTimezoneName": "Europe/Berlin", "regularMarketTime": ts("2026-11-23T17:35:00+01:00"), "regularMarketPrice": 186.34},
       "timestamp": [ts("2026-11-19T09:00:00+01:00"), ts("2026-11-20T09:00:00+01:00")], "indicators": {"quote": [{"close": [187.02, 183.68]}]}}
ser, _ = M.series(res)
assert [d.isoformat() for d, _v in ser] == ["2026-11-19", "2026-11-20", "2026-11-23"] and ser[-1][1] == 186.34, "missing Monday filled from meta"
res["timestamp"].append(ts("2026-11-23T09:00:00+01:00")); res["indicators"]["quote"][0]["close"].append(186.0)
ser, _ = M.series(res)
assert len(ser) == 3 and ser[-1][1] == 186.0, "bars win when the session is there"

# stale fallback: a map that fails keeps the published copy, flagged
M.fetch_all = lambda syms, workers=12: ({}, {})
out = M.build(now=z("2026-11-23T23:30"), base={"maps": {"sp500": dict(m)}}, keys=("sp500", "csi300"), universe=U)
assert out["maps"]["sp500"]["stale"] is True and "csi300" not in out["maps"], "failed map: previous copy, flagged stale; none -> left out"
assert out["schema"] == 1 and out["order"] == ["sp500", "csi300"]

# tiles for the Equities Monitor exist in the universe
em = next(g for g in universe.GROUPS if g["id"] == "equity_monitor")
syms = {r["sym"] for s in em["sections"] for r in s["rows"]}
assert {"^NDX", "^RUT", "^SOX", "MAGS", "^FCHI", "^KS11", "^TWII", "^AXJO", "ES=F", "NQ=F", "^AEX", "^IBEX"} <= syms
idx = {r["sym"] for g in universe.GROUPS if g["id"] == "indices" for s in g["sections"] for r in s["rows"]}
assert not (syms & idx), "no duplicate rows between Indices and the Equities Monitor"
wf = (Path(__file__).resolve().parent.parent / ".github/workflows/intraday.yml").read_text()
assert "build_region_maps.py" in wf and "data/region_maps.json" in wf, "intraday run publishes region_maps.json"
print("region maps ok")
