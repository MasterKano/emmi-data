# og-mining-v2

Free-source oil & gas and mining data for the EMMI v2 Oil & gas and Mining pages.

Universe (Jerry, 12 Nov 2026 02:06 Zurich): 32 oil & gas companies — 17 London (Tullow, Capricorn, Genel, Eco Atlantic, Ithaca, Afentra, Kistos, Jadestone, Chariot, Touchstone, Gulf Keystone, Serica `SER.L`, Energean, Seplat, Rockhopper, EnQuest, Pharos), Sweden (International Petroleum `IPCO.ST`, ShaMaran `SNM-SDB.ST`, Meren `MER.ST`, Africa Energy `AEC.ST`), and the other Norwegian-listed oil E&P names (Equinor, Aker BP, Vår Energi, DNO, BlueNord, BW Energy, OKEA, Panoro, PetroNor, Questerre, Interoil). One line per company; London duals (GKP.OL, Zenith) and Orrön are out. Mining is the six Lundin names only (`LUN.TO`, `LUG.TO`, `NGEX.TO`, `LUC.TO`, `MAU.TO`, `LUNR.TO`).

Site reads from this folder on `main`:

```
https://raw.githubusercontent.com/MasterKano/emmi-data/main/og-mining-v2/<file>.json
```

Live prices still come from `data/market.json` on `live-data`. Policy: null means unavailable; no invented or paid inputs.
