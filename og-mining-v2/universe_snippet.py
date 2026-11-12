# Jerry tighter O&G list, 12 Nov 2026 02:06 Zurich (t1171u), plus Navitas (t585u, 12 Nov 23:04 Zurich). 33 oil & gas + 6 Lundin mining.
# Serica = SER.L. ShaMaran = SNM-SDB.ST (Sweden). Meren = MER.ST (ex-Africa Oil). Orrön excluded.
OIL_GAS_V2 = dict(id="oilgas_v2", label="Oil & gas", sections=[
    dict(label="London", rows=[
        R("TLW.L", "Tullow Oil plc", "LSE:TLW", note="Small E&P: Tullow: Ghana (Jubilee, TEN), Côte d'Ivoire."),
        R("CNE.L", "Capricorn Energy PLC", "LSE:CNE", note="Small E&P: Capricorn: Egypt Western Desert producer."),
        R("GENL.L", "Genel Energy plc", "LSE:GENL", note="Small E&P: Genel: Kurdistan (Sarta) + Somaliland exploration."),
        R("ECO.L", "Eco (Atlantic) Oil & Gas Ltd.", "LSE:ECO", note="Explorer: Eco Atlantic: Guyana, Namibia, South Africa exploration; also TSXV EOG."),
        R("ITH.L", "Ithaca Energy plc", "LSE:ITH", note="Large E&P: UK North Sea producer (Eni UK assets merged in)."),
        R("AET.L", "Afentra plc", "LSE:AET", note="Small E&P: Afentra: Angola producer."),
        R("KIST.L", "Kistos Holdings Plc", "LSE:KIST", note="Small E&P: Gas: UK, Netherlands, Norway."),
        R("JSE.L", "Jadestone Energy plc", "LSE:JSE", note="Small E&P: Jadestone: Australia, Malaysia, Indonesia, Vietnam producer."),
        R("CHAR.L", "Chariot Limited", "LSE:CHAR", note="Explorer: Chariot: Morocco gas, plus power/water."),
        R("TXP.L", "Touchstone Exploration Inc.", "LSE:TXP", note="Small E&P: Touchstone: Trinidad gas & oil; also TSX TXP."),
        R("GKP.L", "Gulf Keystone Petroleum Limited", "LSE:GKP", note="Mid E&P: Kurdistan (Shaikan) producer; also on Euronext Growth Oslo (GKP)."),
        R("SER.L", "Serica Energy plc", "LSE:SER", note="Mid E&P: UK North Sea producer. LSE TIDM is SER (EMMI v1 uses stale SQZ.L)."),
        R("ENOG.L", "Energean plc", "LSE:ENOG", note="Large E&P: Gas producer: Israel (Karish), Egypt, Italy, Croatia."),
        R("SEPL.L", "Seplat Energy Plc", "LSE:SEPL", note="Large E&P: Nigeria onshore + shallow offshore oil & gas; also listed on NGX."),
        R("RKH.L", "Rockhopper Exploration plc", "LSE:RKH", note="Mid E&P: Falklands Sea Lion development + Italy arbitration award."),
        R("ENQ.L", "EnQuest PLC", "LSE:ENQ", note="Mid E&P: UK North Sea + Malaysia producer."),
        R("PHAR.L", "Pharos Energy plc", "LSE:PHAR", note="Small E&P: Pharos: Vietnam + Egypt producer."),
    ]),
    dict(label="Sweden", rows=[
        R("IPCO.ST", "International Petroleum Corp", "OMXSTO:IPCO", note="Large E&P: IPC: Canada (Blackrod), Malaysia, France. Lundin Group. Also TSX IPCO (EMMI v1 uses IPCO.TO)."),
        R("SNM-SDB.ST", "ShaMaran Petroleum", "OMXSTO:SNM_SDB", note="Small E&P: SDB line of ShaMaran (primary Euronext Growth Oslo SNM)."),
        R("AEC.ST", "Africa Energy Corp", "OMXSTO:AEC", note="Explorer: South Africa Block 11B/12B gas-condensate (exploration). Also TSX Venture-listed."),
        R("MER.ST", "Meren Energy", "OMXSTO:MER", note="Mid E&P: Ex-Africa Oil (renamed after Impact Oil & Gas deal): Nigeria (Prime), Namibia, South Africa. Also TSX MER."),
    ]),
    dict(label="Norway", rows=[
        R("EQNR.OL", "Equinor ASA", "OSL:EQNR", note="Integrated major: Norwegian state-controlled major; NCS + international, renewables arm."),
        R("AKRBP.OL", "Aker BP ASA", "OSL:AKRBP", note="Large E&P: NCS pure-play (Johan Sverdrup, Yggdrasil)."),
        R("VAR.OL", "Vår Energi ASA", "OSL:VAR", note="Large E&P: Vår Energi: NCS producer (Eni-controlled)."),
        R("DNO.OL", "DNO ASA", "OSL:DNO", note="Mid E&P: Kurdistan + North Sea/Norway (Sval Energi acquired 2025)."),
        R("BNOR.OL", "BlueNord ASA", "OSL:BNOR", note="Mid E&P: BlueNord: Danish North Sea (Tyra) gas."),
        R("BWE.OL", "BW Energy Limited", "OSL:BWE", note="Mid E&P: BW Energy: Gabon, Brazil, Namibia (FPSO-led developments)."),
        R("OKEA.OL", "OKEA ASA", "OSL:OKEA", note="Mid E&P: NCS mid-size producer."),
        R("PEN.OL", "Panoro Energy ASA", "OSL:PEN", note="Small E&P: Panoro: Equatorial Guinea, Gabon, Tunisia."),
        R("PNOR.OL", "PetroNor E&P ASA", "OSL:PNOR", note="Small E&P: PetroNor: Congo-Brazzaville (PNGF Sud)."),
        R("QEC.OL", "Questerre Energy Corporation", "OSL:QEC", note="Small E&P: Questerre: Quebec/Alberta gas (Utica); primary TSX QEC. QGAS pref line deduped."),
        R("IOX.OL", "Interoil Exploration and Production ASA", "OSL:IOX", note="Small E&P: Interoil: Argentina/Colombia small producer."),
    ]),
    dict(label="Tel Aviv", rows=[
        R("NVPT.TA", "Navitas Petroleum, Limited Partnership", "TASE:NVPT", note="Large exploration and production company: US Gulf of America (Shenandoah, Buckskin, Monument) and Falklands Sea Lion; Eco Atlantic partner on the Orinduik option and Block 1 CBK. Also a thin US OTC line (NVPTF)."),
    ]),
])
LUNDIN_MINING_V2 = dict(id="lundin_mining_v2", label="Mining (Lundin Group)", sections=[dict(label=None, rows=[
    R("LUN.TO", "Lundin Mining", "TSX:LUN", note="Copper (+gold, nickel, zinc): Chile (Candelaria, Caserones), Brazil (Chapada), Argentina (Vicuña JV with BHP); 2025 output ~331kt Cu (company site)."),
    R("LUG.TO", "Lundin Gold", "TSX:LUG", note="Gold: Fruta del Norte, Ecuador; 2025 output 498,315 oz (Lundin Group 2025 review)."),
    R("NGEX.TO", "NGEx Minerals", "TSX:NGEX", note="Copper-gold explorer: Lunahuasi (Argentina). Valiente Resources spin-out effective 12 Nov 2026 (ex-date 12 Nov): expect a price step."),
    R("LUC.TO", "Lucara Diamond", "TSX:LUC", note="Diamonds: Karowe mine, Botswana (underground expansion)."),
    R("MAU.TO", "Montage Gold", "TSX:MAU", note="Gold developer: Koné, Côte d'Ivoire. Aug 2026 deck: first gold Q4 2026 via oxide circuit; 3 Nov 2026 release headline says the plant is already operating above nameplate. Expect a developer-to-producer re-rating window."),
    R("LUNR.TO", "LunR Royalties", "TSX:LUNR", note="Royalties (Cu/Au/Ag): Listed 19 Dec 2025; NSR royalties on Los Helados and Lunahuasi. Not Nasdaq LUNR (Intuitive Machines)."),
])])
