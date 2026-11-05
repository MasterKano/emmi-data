"""Futures contract-month resolution, so every price change compares ONE explicit contract month.

Yahoo's continuous series (BZ=F, NG=F, TTF=F, GC=F ...) switch contract month on their own schedule, and the switch
day then compares two different months (27 Oct 2026: BZ=F went Dec-26 -> Jan-27 and showed -5.5% while each month
fell ~1.5%; NG=F went Nov-26 -> Dec-26 and showed +17% while each month rose 1-5%). Rule (same as the morning brief):
  * active(root, d) = the contract still trading AFTER day d's session, i.e. front(root, d + 1 day). On an expiry
    day the next month is used for both today and the prior close (the expiring contract's last bar is unreliable
    and Yahoo drops an expired month's chart).
  * Energy roots roll at exchange expiry. Metals roll in their liquid-month cycle shortly before first notice
    (2 business days before the last business day of the month before delivery).
  * Weekends only as holidays: close enough, an exchange holiday moves an expiry by a day at most.
Yahoo symbols: <root><month code><yy>.<exchange>, e.g. BZF27.NYM, TTFX26.NYM, GCZ26.CMX."""
import datetime as dt

MC = "FGHJKMNQUVXZ"
ALL = tuple(range(1, 13))
# continuous Yahoo symbol -> (root, exchange suffix, listed months used)
ROOTS = {
    "BZ=F": ("BZ", "NYM", ALL),
    "CL=F": ("CL", "NYM", ALL),
    "NG=F": ("NG", "NYM", ALL),
    "HO=F": ("HO", "NYM", ALL),
    "RB=F": ("RB", "NYM", ALL),
    "TTF=F": ("TTF", "NYM", ALL),
    "GC=F": ("GC", "CMX", (2, 4, 6, 8, 10, 12)),
    "SI=F": ("SI", "CMX", (3, 5, 7, 9, 12)),
    "HG=F": ("HG", "CMX", (3, 5, 7, 9, 12)),
    "PL=F": ("PL", "NYM", (1, 4, 7, 10)),
}


def _bd(d):
    return d.weekday() < 5


def _last_bd(y, m):
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    while not _bd(d):
        d -= dt.timedelta(days=1)
    return d


def _add(y, m, k):
    m0 = y * 12 + (m - 1) + k
    return m0 // 12, m0 % 12 + 1


def _back(d, n):
    """n business days before d."""
    while n:
        d -= dt.timedelta(days=1)
        if _bd(d):
            n -= 1
    return d


def expiry(root, y, m):
    """Last day this module treats the (y, m) contract as tradeable."""
    if root == "BZ":            # ICE Brent: last business day of the 2nd month before delivery
        return _last_bd(*_add(y, m, -2))
    if root in ("HO", "RB"):    # last business day of the month before delivery
        return _last_bd(*_add(y, m, -1))
    if root == "CL":            # 3 business days before the 25th of the prior month (25th itself if a business day)
        py, pm = _add(y, m, -1)
        d = dt.date(py, pm, 25)
        while not _bd(d):
            d -= dt.timedelta(days=1)
        return _back(d, 3)
    if root == "NG":            # 3 business days before the first calendar day of delivery
        return _back(dt.date(y, m, 1), 3)
    if root == "TTF":           # 2 business days before the first calendar day of delivery
        return _back(dt.date(y, m, 1), 2)
    if root in ("GC", "SI", "HG", "PL"):   # liquid month until 2 business days before first notice
        return _back(_last_bd(*_add(y, m, -1)), 2)
    raise ValueError(root)


def front(cont, on, nth=0):
    """(yahoo_symbol, label like 'Dec-26', expiry date) for the nth contract still trading on date `on`."""
    root, ex, months = ROOTS[cont]
    found = -1
    for k in range(0, 40):
        cy, cm = _add(on.year, on.month, k)
        if cm in months and expiry(root, cy, cm) >= on:
            found += 1
            if found == nth:
                return f"{root}{MC[cm - 1]}{cy % 100:02d}.{ex}", dt.date(cy, cm, 1).strftime("%b-%y"), expiry(root, cy, cm)
    raise ValueError(cont)


def active(cont, d, nth=0):
    """The contract still trading after day d's session: front(cont, d + 1 day)."""
    return front(cont, d + dt.timedelta(days=1), nth)


if __name__ == "__main__":   # quick table: python scripts/contracts.py [YYYY-MM-DD]
    import sys
    d = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    for c in ROOTS:
        print(c, *active(c, d))
