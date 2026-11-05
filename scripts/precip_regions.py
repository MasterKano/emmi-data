"""Hydro catchment regions for the Nordic precipitation view: a few ERA5 grid points per region (lat, lon).
Points sit in the main regulated catchments (reservoir areas), not at the coast."""
REGIONS = {
    "NO_S": dict(name="Southern Norway", zones="NO2, NO5", pts=[(59.10, 7.10), (59.70, 6.90), (60.30, 7.40), (61.10, 7.60)]),
    "NO_C": dict(name="Central & eastern Norway", zones="NO1, NO3", pts=[(61.20, 9.60), (62.30, 9.40), (63.20, 11.30)]),
    "NO_N": dict(name="Northern Norway", zones="NO4", pts=[(66.30, 14.30), (67.60, 16.00), (68.70, 18.30)]),
    "SE_N": dict(name="Northern Sweden", zones="SE1, SE2", pts=[(67.30, 18.40), (65.90, 16.20), (64.70, 15.00), (63.40, 13.60)]),
}
