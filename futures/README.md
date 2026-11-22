# Futures chains and contract history

Built by `scripts/build_futures.py` (workflow `futures.yml`). Read by the Futures section of the EMMi commodity sheet
(between the price chart and the trader report).

```
futures/index.json               EMMi tile symbol -> id, as-of date, number of listed months, history source
futures/<id>.json                listed months (nearest first, up to 18 that trade) + 70-session closes matrix
futures/<id>/<CONTRACT>.csv      date,open,high,low,close,volume,oi,source   (one row per session)
```

Sources (free, public, delayed; no login or key):

- Chain: TradingView public futures scanner. Last, change, open/high/low, volume, open interest, expiry.
- History: Yahoo Finance per-contract daily bars (e.g. `CLF27.NYM`, `KCH27.NYB`, `ZCH27.CBT`). Brent uses NYMEX `BZ`
  (Brent last-day financial, settles on ICE Brent: same month, same price). TTF uses Yahoo's TTF contracts (same
  prices as ICE Endex TFM). EU carbon (ICE Endex ECF) and lumber have no usable Yahoo contract history, so their
  history is the daily scanner bar archived here from 20 Nov 2026 on. Some far TTF months are missing on Yahoo and
  also start from 20 Nov 2026.

Rules: blanks are "not available", never zero, never interpolated or carried forward. A Yahoo bar replaces a scanner
bar for the same date (Yahoo ranks higher); open interest is taken from the scanner. An unchanged scanner snapshot
(exchange holiday) is not archived as a new session. A market that fails keeps its last good files.
