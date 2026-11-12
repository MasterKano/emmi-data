# og-mining-v2

Free-source oil & gas and mining data for the EMMI v2 Oil & gas and Mining pages.

Universe (Jerry, 12 Nov 2026 02:06 Zurich, plus Navitas at 23:04 the same day): 33 oil and gas companies — 17 London (Tullow, Capricorn, Genel, Eco Atlantic, Ithaca, Afentra, Kistos, Jadestone, Chariot, Touchstone, Gulf Keystone, Serica `SER.L`, Energean, Seplat, Rockhopper, EnQuest, Pharos), Sweden (International Petroleum `IPCO.ST`, ShaMaran `SNM-SDB.ST`, Meren `MER.ST`, Africa Energy `AEC.ST`), the other Norwegian-listed oil E&P names (Equinor, Aker BP, Vår Energi, DNO, BlueNord, BW Energy, OKEA, Panoro, PetroNor, Questerre, Interoil), and Tel Aviv (Navitas Petroleum `NVPT.TA`). One line per company; London duals (GKP.OL, Zenith) and Orrön are out. Mining is the six Lundin names only (`LUN.TO`, `LUG.TO`, `NGEX.TO`, `LUC.TO`, `MAU.TO`, `LUNR.TO`).

Site reads from this folder on `main`:

```
https://raw.githubusercontent.com/MasterKano/emmi-data/main/og-mining-v2/<file>.json
```

Live prices still come from `data/market.json` on `live-data`. Policy: null means unavailable; no invented or paid inputs.

## annual_energy.json

Energy Institute Statistical Review of World Energy 2026 (formerly BP), data to 2025, published 30 Jun 2026. Fetched via the unmodified OWID meadow parquet. Population is EI's own series. Oil and gas per capita are computed from EI consumption and EI population (barrels or m³ per person per year). Total-energy per capita is EI's `tes_gj_pc`. There is no official EI total-energy production total — production is shown for oil and for gas; total energy is TES (supply/consumption).

GDP is World Bank WDI constant 2015 US dollars, country-level only (NY.GDP.MKTP.KD lastupdated 2026-10-08; NY.GDP.PCAP.KD lastupdated 2026-11-11). Regional GDP is null: Bank region definitions must not be mixed with EI regions (in particular, do not divide EI "Total Asia Pacific" by World Bank "East Asia & Pacific", which excludes India).

129 entities (18 regions, 111 countries), 1965–2025. Null means unavailable. No interpolation.

Observed 2000 → 2025 (for takeaways; recompute from the file before publishing prose):

- World oil per capita almost flat: 4.54 → 4.57 barrels/person/year. TES 61.8 → 72.9 GJ.
- Total Europe oil 9.27 → 7.65; TES 132.6 → 105.9 GJ (declining).
- Total EU oil 11.02 → 8.67; TES 147.1 → 116.2 GJ (declining).
- Total Asia Pacific oil 2.23 → 3.30; TES 31.8 → 64.6 GJ (rising).
- China oil 1.34 → 4.48 (still below Europe); TES 32.2 → 114.5 GJ — China's total-energy per capita is now above Europe's 105.9. Say that, not "catching up".
- India oil 0.79 → 1.41; TES 12.2 → 26.7 GJ (still low; 2025 oil per capita slightly down year-on-year).
- US oil 25.42 → 20.41; TES 335.8 → 270.2 GJ (down, still the highest of this set).
- Germany oil 12.24 → 8.86; TES 174.0 → 119.5 GJ (declining).
