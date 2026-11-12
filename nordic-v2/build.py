#!/workspace/brief/.venv/bin/python
"""Box-only Nordic v2 data build. Never modifies EMMI, Pulse, masters or routines."""
from __future__ import annotations
import argparse, calendar, datetime as dt, hashlib, json, math, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests

BASE=Path(__file__).resolve().parent
CACHE=BASE/'cache'
OUT=BASE/'output'
CLIMATE=Path('/home/box/data/nordic-climate')
SCRIPTS=Path('/workspace/emmi-data/scripts')
sys.path.insert(0,str(SCRIPTS))
import entsoe_client as ec
import build_power as bp
sys.path.insert(0,str(CLIMATE))
import daily_products as climate
COUNTRIES={'SE':'10YSE-1--------K','NO':'10YNO-0--------C','FI':'10YFI-1--------U','DK':'10Y1001A1001A65H'}
ZONES={k:v for k,v in ec.ENTSOE_EIC.items() if k[:2] in COUNTRIES}
REGIONS=['Nordic','SE','NO','FI','DK']
TODAY=dt.datetime.now(ec.CET).date()
ERRORS=[]
BASELINE=(2006,2025)
PRICE_HISTORY_START=dt.date(2021,1,1)
PRICE_BASELINE=(2021,2025)
PRICE_MTU_SWITCH=dt.date(2025,10,1)  # first 15-minute SDAC delivery day

def log(s): print(ec._redact(s),flush=True)
def clean(v):
    if isinstance(v,dict):return {str(k):clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [clean(x) for x in v]
    if isinstance(v,(np.integer,)):return int(v)
    if isinstance(v,(float,np.floating)):return round(float(v),3) if np.isfinite(v) else None
    if isinstance(v,(pd.Timestamp,dt.date,dt.datetime)):return v.isoformat()
    return v

def write(name,obj):
    obj={'schema_version':1,**obj}
    text=json.dumps(clean(obj),separators=(',',':'),ensure_ascii=False,allow_nan=False)
    tmp=OUT/(name+'.tmp');tmp.write_text(text);tmp.replace(OUT/name)
    log(f'{name}: {len(text):,} bytes')
    return obj

def get_json(url,params=None,cache_key=None,max_age=0):
    dest=CACHE/(cache_key+'.json') if cache_key else None
    if dest and dest.exists() and time.time()-dest.stat().st_mtime<max_age:
        return json.loads(dest.read_text())
    r=requests.get(url,params=params,timeout=90);r.raise_for_status();j=r.json()
    if dest:dest.write_text(json.dumps(j,separators=(',',':')))
    return j

def query(params,key,final=False,max_age=None,tries=2):
    p=CACHE/(key+'.xml')
    ttl=6*3600 if max_age is None else max_age
    if p.exists() and (final or time.time()-p.stat().st_mtime<ttl):
        text=p.read_text()
    else:
        text=ec.entsoe_raw(params,tries=tries).text
        root=ec._strip_ns(text);ec._ack(root)
        p.write_text(text)
    return text

def safe(job,fn):
    try:return fn()
    except Exception as e:
        msg=ec._redact(e)[:180];ERRORS.append({'part':job,'error':msg});log(f'{job}: {msg}');return None


def is_stub(s):
    # A03 single-point zero placeholders look like a complete 0 MW series after expansion.
    if s is None or len(s.dropna())==0:return True
    v=s.dropna();return bool((v==0).all() and v.nunique()<=1)

def hourly_idx(items,idx):
    s=ec._combine(items)
    if s is None:return [None]*len(idx)
    r=s.resample('1h');h=r.mean();step=min(x['step'] for x in items);h=h.where(r.count()>=max(1,60//step))
    return none_list(h.reindex(idx).tolist())

def forecast_from_items(items,idx):
    out={}
    for k,psrs in [('wind_fc',['B18','B19']),('solar_fc',['B16'])]:
        parts=[]
        for p in psrs:
            it=[i for i in items if i['psr']==p]
            if not it:continue
            if is_stub(ec._combine(it)):continue
            parts.append(hourly_idx(it,idx))
        out[k]=none_list(pd.DataFrame(parts).sum(axis=0,min_count=len(parts)).tolist()) if parts else [None]*len(idx)
    return out

def entsoe_wind_solar(label,eic,process,target,idx,cache_key):
    text=safe(label,lambda:query(dict(documentType='A69',processType=process,in_Domain=eic,**ec._period(target,target+dt.timedelta(days=1))),cache_key,max_age=0))
    items=ec.entsoe_parse(text) if text else []
    kind='day-ahead' if process=='A01' else 'intraday'
    return forecast_from_items(items,idx),f'ENTSO-E A69 {process} ({kind})'

def none_list(vals):
    return [None if v is None or pd.isna(v) else v for v in vals]

def fill_if_missing(dst,src,sources,key,source):
    cur,new=none_list(dst[key]),none_list(src[key])
    if all(v is None for v in cur) and any(v is not None for v in new):
        dst[key]=new;sources[key]=source

def fingrid_forecast(idx):
    keys=[k for k in ((os.environ.get(v) or '').strip() for v in ('FINGRID_API_KEY','FINGRID_API_KEY_SECONDARY')) if k]
    if not keys:return None
    a,b=idx[0],idx[-1]+pd.Timedelta(hours=1)
    out={}
    # 245/248 are the rolling 72-hour forecasts and currently cover the full delivery day;
    # day-ahead 246/247 stop one hour early.
    for i,(ds,k) in enumerate(((245,'wind_fc'),(248,'solar_fc'))):
        if i:time.sleep(6.5)
        r=bp.get(f'{bp.FINGRID_API}{ds}/data',tries=2,headers={'x-api-key':keys[0],'Accept':'application/json'},
                 params=dict(startTime=bp._ts(a-pd.Timedelta(hours=2)),endTime=bp._ts(b+pd.Timedelta(hours=2)),pageSize=20000,sortOrder='asc',format='json'))
        js=r.json();rows=js.get('data') if isinstance(js,dict) else js
        if not rows:raise LookupError(f'Fingrid {ds}: no data')
        s=pd.Series([x['value'] for x in rows],index=pd.to_datetime([x['startTime'] for x in rows],utc=True),dtype=float).sort_index()
        out[k]=none_list(s.resample('1h').mean().reindex(idx).tolist())
    return out

def energinet_forecast(idx,target):
    a=idx[0]-pd.Timedelta(hours=2);b=idx[-1]+pd.Timedelta(hours=14)
    fc=bp._eds('Forecasts_Hour',a,b,{'PriceArea':['DK1','DK2'],'ForecastType':['Offshore Wind','Onshore Wind','Solar']},
               ['HourUTC','PriceArea','ForecastType','ForecastDayAhead'])
    if fc is None or fc.empty:return {}
    fc=fc.assign(t=pd.to_datetime(fc['HourUTC'],utc=True))
    out={}
    for z in ['DK1','DK2']:
        y=fc[fc['PriceArea']==z]
        if y.empty:continue
        p=y.pivot_table(index='t',columns='ForecastType',values='ForecastDayAhead',aggfunc='last')
        res={'wind_fc':[None]*len(idx),'solar_fc':[None]*len(idx)}
        wcols=[c for c in ('Offshore Wind','Onshore Wind') if c in p.columns]
        if wcols:res['wind_fc']=none_list(p[wcols].sum(axis=1,min_count=len(wcols)).resample('1h').mean().reindex(idx).tolist())
        if 'Solar' in p.columns:res['solar_fc']=none_list(p['Solar'].resample('1h').mean().reindex(idx).tolist())
        out[z]=res
    return out

def fill_dk_wind(frames,denominators,daily_cap,latest,out):
    if 'DK' not in frames:return []
    g=frames['DK'];last=g['wind'].dropna().index.max() if 'wind' in g and g['wind'].notna().any() else None
    if last is not None and last.date()>=TODAY-dt.timedelta(days=1):return []
    start=(last.date()+dt.timedelta(days=1)) if last is not None else TODAY-dt.timedelta(days=14)
    a,_=ec._cet_bounds(start,start+dt.timedelta(days=1));_,b=ec._cet_bounds(TODAY,TODAY+dt.timedelta(days=1))
    act=bp._eds('ElectricityProdex5MinRealtime',a,min(pd.Timestamp(dt.datetime.now(dt.timezone.utc)),pd.Timestamp(b)),
                {'PriceArea':['DK1','DK2']},['Minutes5UTC','PriceArea','OffshoreWindPower','OnshoreWindPower'])
    if act is None or act.empty:return ['DK wind after ENTSO-E cutoff: Energinet returned no actuals']
    act=act.assign(t=pd.to_datetime(act['Minutes5UTC'],utc=True))
    act['wind']=act[['OffshoreWindPower','OnshoreWindPower']].astype(float).sum(axis=1,min_count=1)
    p=act.pivot_table(index='t',columns='PriceArea',values='wind',aggfunc='last')
    if not {'DK1','DK2'}.issubset(p.columns):return ['DK wind after ENTSO-E cutoff: Energinet missing a bidding zone']
    tot=p[['DK1','DK2']].sum(axis=1,min_count=2).sort_index()
    added=[]
    for day,s in tot.groupby(tot.index.tz_convert(ec.CET).strftime('%Y-%m-%d')):
        d=dt.date.fromisoformat(day);lo,hi=ec._cet_bounds(d,d+dt.timedelta(days=1))
        need=int((hi-lo).total_seconds()/300)
        part=s.loc[(s.index>=lo)&(s.index<hi)]
        if part.notna().sum()!=need:continue
        ts=pd.Timestamp(d)
        if ts not in g.index:g.loc[ts,g.columns]=np.nan
        if pd.isna(g.loc[ts,'wind']):
            g.loc[ts,'wind']=float(part.mean());added.append(str(d))
    g.sort_index(inplace=True)
    frames['DK']=g
    path=CACHE/f'gen_daily_v2_DK_{TODAY.year}.csv'
    g[['wind','hydro','nuclear']].rename_axis('date').to_csv(path)
    if not added:return ['DK wind after ENTSO-E cutoff: Energinet actuals had no complete extra delivery days']
    cap=denominators['DK']
    if 'DK' in daily_cap:
        cap=daily_cap['DK'].reindex(g.index).combine_first(cap.reindex(g.index))
    else:
        cap=cap.reindex(g.index)
    denominators['DK']=cap
    g['wind_load_factor']=100*g.wind/cap
    g.loc[(g.wind_load_factor<0)|(g.wind_load_factor>110),'wind_load_factor']=np.nan
    frames['DK']=g
    out['capacity_daily']['DK']={'columns':['date','wind_mw'],'rows':[[str(t.date()),v] for t,v in cap.items() if t>=pd.Timestamp(TODAY-dt.timedelta(days=122))],'latest_source':latest['DK']}
    out['source']='ENTSO-E A75 actual generation; A68 annual installed capacity; Energinet ElectricityProdex5MinRealtime for recent DK wind where A75 is incomplete'
    return [f"DK wind {added[0]}..{added[-1]} from Energinet ElectricityProdex5MinRealtime (DK1+DK2); ENTSO-E A75 currently ends 2026-11-04 07:45Z"]

def trailing(s):
    if s.empty:return s
    return s.reindex(pd.date_range(s.index.min(),s.index.max())).rolling(30,min_periods=30).mean()

def summary(v,n,unit):
    return dict(value=v,normal=n,difference=(v-n if pd.notna(v) and pd.notna(n) else None),
                difference_unit=unit,
                difference_pct=((v/n-1)*100 if unit!='degC' and pd.notna(v) and pd.notna(n) and n!=0 else None))

def weather():
    log('weather: loading sole daily_products country builder')
    boundaries=CACHE/'countries.geojson'
    if not boundaries.exists():
        r=requests.get('https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson',timeout=60);r.raise_for_status();boundaries.write_text(r.text)
    d,meta=climate.regional_raw_daily(CACHE/'countries.geojson',CACHE/'regional_daily.csv')
    result={'as_of':str(d.date.max().date()),'baseline':list(BASELINE),'history_start':meta['history_start'],
            'units':{'wind100':'m/s at 100m','temp':'degC at 2m','rain':'mm/day'},
            'source':'https://archive-api.open-meteo.com/v1/archive?models=era5',
            'method':meta['method'],'grid':meta['grid'],'point_counts':meta['points'],
            'columns':['date','value','ma30','normal','normal_ma30'],'regions':{}}
    normals={}
    for c,g in d.groupby('region'):
        g=g.sort_values('date').set_index('date');idx=pd.date_range(g.index.min(),g.index.max())
        g=g.reindex(idx);b=g.loc[f'{BASELINE[0]}-01-01':f'{BASELINE[1]}-12-31']
        expected=pd.date_range(f'{BASELINE[0]}-01-01',f'{BASELINE[1]}-12-31')
        if len(b)!=len(expected) or b[list(climate.u.VARS)].isna().any().any():raise ValueError(f'incomplete baseline {c}')
        norm=b.groupby(b.index.strftime('%m-%d'))[list(climate.u.VARS)].mean()
        normals[c]=norm
        reg={'as_of':str(g[list(climate.u.VARS)].dropna().index.max().date()),'metrics':{}}
        for k in climate.u.VARS:
            s=g[k];n=pd.Series([norm.loc[t.strftime('%m-%d'),k] for t in idx],index=idx)
            v30,n30=trailing(s),trailing(n)
            end=s.dropna().index.max();start=end-pd.Timedelta(days=122)
            dates=idx[(idx>=start)&(idx<=end)]
            reg['metrics'][k]={'as_of':str(end.date()),'history_start':str(s.dropna().index.min().date()),'summary':summary(v30.loc[end],n30.loc[end],result['units'][k]),
                               'rows':[[str(t.date()),s.loc[t],v30.loc[t],n.loc[t],n30.loc[t]] for t in dates]}
        result['regions'][c]=reg
    write('weather.json',result)
    # Full 366-day normals are small and keep forecast comparisons on the same country masks.
    write('weather_normals.json',{'as_of':f'{BASELINE[1]}-12-31','baseline':list(BASELINE),
          'columns':['month_day','wind100','temp','rain'], 'regions':{c:[[md,*row.tolist()] for md,row in n.iterrows()] for c,n in normals.items()},
          'source':result['source'],'method':result['method']})
    return normals,meta

def daily_series(items,psrs=None):
    by={}
    for it in items:
        if not it['is_in'] or (psrs and it['psr'] not in psrs):continue
        by.setdefault(it['psr'],[]).append(it)
    columns=[]
    for psr,parts in by.items():
        # Revisions are alternative series, never additive. Independent PSR types sum only after alignment.
        s=ec._combine(parts).sort_index();step=min(i['step'] for i in parts)
        h=s.reindex(pd.date_range(s.index.min(),s.index.max()+pd.Timedelta(minutes=step),freq='15min',inclusive='left'),method='ffill',tolerance=pd.Timedelta(minutes=step-15)) if step>=15 else s.resample('15min').mean()
        grouped=h.tz_convert(ec.CET).groupby(h.index.tz_convert(ec.CET).strftime('%Y-%m-%d'))
        vals={}
        for ds,g in grouped:
            day=dt.date.fromisoformat(ds);a,b=ec._cet_bounds(day,day+dt.timedelta(days=1));need=int((b-a).total_seconds()/900)
            if g.notna().sum()==need:vals[pd.Timestamp(ds)]=g.mean()
        columns.append(pd.Series(vals,dtype=float))
    if not columns:return pd.Series(dtype=float)
    frame=pd.concat(columns,axis=1)
    return frame.sum(axis=1,min_count=len(columns)).sort_index()

def price_windows(start,end):
    """Inclusive start, exclusive end. Yearly chunks, split at the Oct 2025 15-minute switch
    so daily_series never combines hourly and 15-minute series in one window."""
    cuts={start,end}
    for y in range(start.year+1,end.year+1):
        d=dt.date(y,1,1)
        if start<d<end:cuts.add(d)
    if start<PRICE_MTU_SWITCH<end:cuts.add(PRICE_MTU_SWITCH)
    cuts=sorted(cuts)
    return [(a,b,b<=dt.date(TODAY.year,1,1)) for a,b in zip(cuts,cuts[1:]) if a<b]

def _next_month(d):
    return dt.date(d.year+1,1,1) if d.month==12 else dt.date(d.year,d.month+1,1)

def fetch_price_window(z,e,a,b,final):
    """Daily CET delivery-day series for [a,b). On a long-window failure, split into months."""
    key=f'price_{z}_{a}_{b}'
    csv=CACHE/f'price_daily_{z}_{a}_{b}.csv'
    if final and csv.exists():
        s=pd.read_csv(csv,parse_dates=['date'],index_col='date')['value']
        s.index=pd.to_datetime(s.index).tz_localize(None)
        return s,None
    try:
        text=query(dict(documentType='A44',in_Domain=e,out_Domain=e,**ec._period(a,b)),key,final,tries=4)
        items=ec.entsoe_parse(text)
        # A window can mix hourly and 15-minute days (Norwegian 2025). Average each resolution
        # separately so daily_series does not drop complete hourly days.
        by={}
        for it in items:by.setdefault(it['step'],[]).append(it)
        parts=[daily_series(group) for group in by.values()]
        daily=pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)
        daily=daily[~daily.index.duplicated(keep='last')]
        if final:
            pd.Series(daily,name='value').to_csv(csv,index_label='date')
        return daily,items
    except Exception as err:
        if (b-a).days<=32:raise
        log(f'price {z} {a}..{b} failed ({ec._redact(err)[:100]}); splitting into months')
        parts=[];items=None
        cur=a
        while cur<b:
            nxt=min(b,_next_month(cur))
            d,items=fetch_price_window(z,e,cur,nxt,final and nxt<=dt.date(TODAY.year,1,1))
            if d is not None and len(d):parts.append(d)
            cur=nxt
        daily=pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)
        daily=daily[~daily.index.duplicated(keep='last')]
        if final and len(daily):pd.Series(daily,name='value').to_csv(csv,index_label='date')
        return daily,items

def missing_days(s,start,end):
    if end<start:return []
    have=set(pd.Timestamp(t).normalize() for t in s.index)
    return [str(d.date()) for d in pd.date_range(start,end) if d not in have]

def seasonal_normal(s,y0,y1):
    b=s.loc[f'{y0}-01-01':f'{y1}-12-31'].dropna()
    if b.empty:return pd.Series(dtype=float)
    return b.groupby(b.index.strftime('%m-%d')).mean()

def month_series(s):
    s=s.dropna().copy();s.index=pd.to_datetime(s.index)
    if s.empty:return pd.Series(dtype=float)
    return s.groupby(s.index.to_period('M')).mean()

def system_price_history():
    parts=[];blocked=[]
    for y in range(PRICE_HISTORY_START.year,TODAY.year+1):
        final=y<TODAY.year
        key=f'sys_{y}' if final else f'sys_{TODAY}'
        dest=CACHE/(key+'.json')
        if dest.exists() and (final or time.time()-dest.stat().st_mtime<21600):
            js=json.loads(dest.read_text())
        else:
            try:
                r=requests.get(bp.NP_API+'AggregatePrices',params=dict(year=y,market='DayAhead',deliveryArea='SYS',currency='EUR'),
                               headers=getattr(bp,'NP_HDR',{'Accept':'application/json'}),timeout=90)
                if r.status_code==401:
                    blocked.append(y);log(f'system price {y}: Nord Pool AggregatePrices HTTP 401 (not available for this year)');continue
                r.raise_for_status();js=r.json();dest.write_text(json.dumps(js,separators=(',',':')))
            except Exception as e:
                safe(f'system price {y}',lambda e=e:(_ for _ in ()).throw(e))
                continue
        vals={pd.Timestamp(x['deliveryStart'][:10]):x['averagePerArea'].get('SYS')
              for x in js.get('multiAreaDailyAggregates',[])
              if x.get('averagePerArea',{}).get('SYS') is not None and x['deliveryStart'][:10]<=str(TODAY)}
        if vals:parts.append(pd.Series(vals,dtype=float))
    if blocked:log(f'system price: years {blocked[0]}–{blocked[-1]} unavailable from Nord Pool; Nordic history uses later years only')
    if not parts:return pd.Series(dtype=float)
    s=pd.concat(parts).sort_index();return s[~s.index.duplicated(keep='last')]

def prices():
    jobs=list(ZONES.items());series={};history={};end=TODAY+dt.timedelta(days=2)
    windows=price_windows(PRICE_HISTORY_START,end)
    log('prices: ENTSO-E A44 '+str(PRICE_HISTORY_START)+' to '+str(end)+', '+str(len(windows))+' windows per zone')
    def one(item):
        z,e=item;parts=[];hourly=None
        for a,b,final in windows:
            daily,items=fetch_price_window(z,e,a,b,final)
            if daily is not None and len(daily):parts.append(daily)
            if items and b>TODAY:hourly=ec._combine(items)
        daily=pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)
        daily=daily[~daily.index.duplicated(keep='last')]
        daily=daily[daily.index.date<=TODAY]
        if hourly is None and len(daily):
            # Last-resort hourly window if the current-year file was served from daily CSV only.
            text=query(dict(documentType='A44',in_Domain=e,out_Domain=e,**ec._period(TODAY-dt.timedelta(days=3),end)),f'price_{z}_hourly_{TODAY}',tries=4)
            hourly=ec._combine(ec.entsoe_parse(text))
        return z,hourly,daily
    for item in ec.pmap(lambda x:safe('price '+x[0],lambda:one(x)),jobs,4):
        if item:
            series[item[0]]=(item[1],item[2]);history[item[0]]=item[2]
    sys_s=system_price_history()
    if len(sys_s):history['Nordic']=sys_s[sys_s.index.date<=TODAY]
    for c,zz in {'SE':['SE1','SE2','SE3','SE4'],'NO':['NO1','NO2','NO3','NO4','NO5'],'DK':['DK1','DK2']}.items():
        if all(z in history for z in zz):
            f=pd.concat([history[z] for z in zz],axis=1);s=f.mean(axis=1).where(f.notna().all(axis=1))
            history[c]=s[s.index.date<=TODAY].dropna() if len(s) else pd.Series(dtype=float)
    definitions={'Nordic':'official unconstrained Nordic system price, not country average',
                 'SE':'equal-weight average of bidding zones, not load-weighted',
                 'NO':'equal-weight average of bidding zones, not load-weighted',
                 'DK':'equal-weight average of bidding zones, not load-weighted'}
    y0,y1=PRICE_BASELINE
    label=f'{y0}–{y1}'
    method=('Unsmoothed average of the same month/day in '+label+'. Feb 29 uses the available leap years. '
            'A day is the mean of all complete Europe/Zurich delivery-hour or 15-minute prices. '
            'Country values are equal-weight bidding-zone averages, not load-weighted. '
            'Nordic is the official Nord Pool system price.')
    out={'as_of':None,'units':'EUR/MWh','columns':['date','value','ma30'],
         'source':'ENTSO-E A44; system price Nord Pool',
         'baseline':list(PRICE_BASELINE),'baseline_label':label,
         'history_start':str(PRICE_HISTORY_START),'normal_method':method,
         'regions':{}}
    normals={};monthly_months=None;monthly={};monthly_normal={}
    period_end=None
    for code in ['NO1','NO2','NO3','NO4','NO5','SE1','SE2','SE3','SE4','FI','DK1','DK2','Nordic','SE','NO','DK']:
        s=history.get(code)
        if s is None or not len(s):continue
        s=s.sort_index();s=s[~s.index.duplicated(keep='last')];s=s[s.index.date<=TODAY].dropna()
        if not len(s):continue
        as_of=s.index.max().date();start=s.index.min().date()
        expected_end=min(TODAY,as_of)
        gaps=missing_days(s,max(PRICE_HISTORY_START,start),expected_end)
        ma=trailing(s);norm=seasonal_normal(s,y0,y1);normals[code]=norm
        n=pd.Series([norm.loc[t.strftime('%m-%d')] if t.strftime('%m-%d') in norm.index else float('nan') for t in s.index],index=s.index)
        n30=trailing(n)
        end_t=s.index.max()
        b0=int(s.loc[f'{y0}-01-01':f'{y1}-12-31'].index.min().year) if len(s.loc[f'{y0}-01-01':f'{y1}-12-31']) else y0
        b1=int(s.loc[f'{y0}-01-01':f'{y1}-12-31'].index.max().year) if len(s.loc[f'{y0}-01-01':f'{y1}-12-31']) else y1
        rec={'as_of':str(as_of),'history_start':str(start),'baseline':[b0,b1],'baseline_label':f'{b0}–{b1}',
             'coverage':{'days':int(s[s.index>=pd.Timestamp(PRICE_HISTORY_START)].shape[0]),
                         'gap_count':len(gaps),'gaps':gaps},
             'rows':[[str(t.date()),s.loc[t],ma.get(t)] for t in s.index[s.index>=pd.Timestamp(TODAY-dt.timedelta(days=122))]]}
        if code in definitions:rec['definition']=definitions[code]
        if end_t in ma.index and end_t in n30.index:
            rec['summary']=summary(ma.loc[end_t],n30.loc[end_t],'EUR/MWh')
        out['regions'][code]=rec
        ms=month_series(s)
        if len(ms):
            months=[str(p) for p in ms.index]
            monthly[code]=[None if pd.isna(v) else float(v) for v in ms.values]
            if monthly_months is None:monthly_months=months
            elif months!=monthly_months:
                # Align later; stash with its own index
                monthly[code]=(months,monthly[code])
            by={}
            for p,v in ms.items():
                if p.year<y0 or p.year>y1:continue
                if p.year==TODAY.year:continue
                by.setdefault(p.month,[]).append(float(v))
            monthly_normal[code]={f'{m:02d}':{'normal':(sum(vs)/len(vs) if vs else None),'min':(min(vs) if vs else None),
                                              'max':(max(vs) if vs else None),'n_years':len(vs)} for m,vs in by.items()}
        period_end=as_of if period_end is None else max(period_end,as_of)
    # Harmonise monthly arrays onto a shared month index.
    if monthly:
        all_months=sorted({m for v in monthly.values() for m in (v[0] if isinstance(v,tuple) else monthly_months or [])})
        if not all_months:
            all_months=monthly_months or []
        aligned={}
        for code,v in monthly.items():
            if isinstance(v,tuple):
                d=dict(zip(*v))
            else:
                d=dict(zip(monthly_months,v))
            aligned[code]=[d.get(m) for m in all_months]
        monthly,monthly_months=aligned,all_months
    out['as_of']=max((r['as_of'] for r in out['regions'].values()),default=None)
    eex=safe('EEX futures',bp.build_eex)
    out['futures']={}
    if eex:
        for k,lab in [('M','front_month'),('Q','next_quarter')]:
            contract=eex['bench'].get(k)
            out['futures'][lab]={'as_of':eex['asof'],'contract':contract,'unit':'EUR/MWh','regions':{z:v.get(contract) for z,v in eex['zones'].items() if z in ['Nordic',*ZONES]}}
    write('prices.json',out)
    md=sorted({k for n in normals.values() for k in n.index})
    # Compact: one month-day index, then a value array per region. Feb 29 is kept for leap years.
    notes=[]
    nord=history.get('Nordic')
    if nord is not None and len(nord) and nord.index.min().date()>PRICE_HISTORY_START:
        notes.append('Nordic (official Nord Pool system price) history starts '+str(nord.index.min().date())+'; Nord Pool AggregatePrices returns HTTP 401 for SYS years before 2024. Bidding-zone normals use 2021–2025. Nordic uses the years actually on file, currently 2024–2025.')
    write('prices_normals.json',{'as_of':f'{y1}-12-31','baseline':list(PRICE_BASELINE),'baseline_label':label,
          'history_start':str(PRICE_HISTORY_START),'history_end':str(period_end or TODAY),
          'units':'EUR/MWh','source':out['source'],'method':method,'notes':notes,
          'month_days':md,
          'normal':{code:[None if k not in n.index or pd.isna(n.loc[k]) else float(n.loc[k]) for k in md] for code,n in normals.items()},
          'months':monthly_months or [],'monthly':monthly,'monthly_normal':monthly_normal,
          'columns':{'normal':['month_days','normal[region]'],'monthly':['months','monthly[region]'],
                     'monthly_normal':'monthly_normal[region][MM] with normal, min, max, n_years'}})
    return series


def capacity(c,y):
    text=query(dict(documentType='A68',processType='A33',in_Domain=COUNTRIES[c],**ec._period(dt.date(y,1,1),dt.date(y+1,1,1))),f'capacity_{c}_{y}',y<TODAY.year)
    root=ec._strip_ns(text);ec._ack(root);by={}
    for ts in root.iter('TimeSeries'):
        psr=ts.findtext('MktPSRType/psrType')
        pts=[float(p.findtext('quantity')) for p in ts.iter('Point') if p.findtext('quantity') is not None]
        if pts:by[psr]=pts[-1]
    wind=sum(by.get(p,0) for p in ['B18','B19']) if any(p in by for p in ['B18','B19']) else None
    return {'year':y,'as_of':f'{y}-01-01','valid_through':f'{y}-12-31','wind_mw':wind,'nuclear_mw':by.get('B14'),
            'source':'ENTSO-E A68/A33 country aggregate; annual year-ahead reported installed capacity',
            'source_url':'https://transparency.entsoe.eu/generation/r2/installedGenerationCapacityAggregation/show','psr_mw':by}
def capacity_supplement():
    """Dated, national fallbacks/coverage checks; secrets stay in environment."""
    latest={};daily={};zones={}
    zonefile=CACHE/f'capacity_zones_{TODAY.year}.json'
    if not zonefile.exists() or time.time()-zonefile.stat().st_mtime>86400:
        data={}
        def zcap(item):
            z,e=item
            text=query(dict(documentType='A68',processType='A33',in_Domain=e,**ec._period(dt.date(TODAY.year,1,1),dt.date(TODAY.year+1,1,1))),f'zone_capacity_{z}_{TODAY.year}')
            root=ec._strip_ns(text)
            return z,{x.findtext('MktPSRType/psrType'):float(x.findtext('.//quantity')) for x in root.iter('TimeSeries') if x.findtext('.//quantity')}
        for row in ec.pmap(lambda item:safe('zone capacity '+item[0],lambda:zcap(item)),[(z,e) for z,e in ZONES.items() if z!='FI'],4):
            if row:data[row[0]]=row[1]
        zonefile.write_text(json.dumps(data))
    if zonefile.exists():
        raw=json.loads(zonefile.read_text())
        for z,v in raw.items():
            zones[z]={'as_of':f'{TODAY.year}-01-01','source':'ENTSO-E A68/A33 annual bidding-zone capacity','psr_mw':v,
                      'wind_mw':sum(v.get(p,0) for p in ['B18','B19']) if any(p in v for p in ['B18','B19']) else None,
                      'note':'Separate reporting scope; do not sum to replace a missing country aggregate without national reconciliation.'}
    key=os.environ.get('FINGRID_API_KEY')
    if key:
        import build_fi_wind as fi
        file=CACHE/f'fi_capacity_{TODAY.year}.json'
        if file.exists() and time.time()-file.stat().st_mtime<86400:
            raw=json.loads(file.read_text())
        else:
            raw=safe('Fingrid capacity fallback',lambda:fi.fetch(key,268,dt.datetime(TODAY.year,1,1,tzinfo=dt.timezone.utc),dt.datetime.now(dt.timezone.utc)))
            if raw:file.write_text(json.dumps(raw))
        if raw:
            s=pd.Series([r['value'] for r in raw],index=pd.to_datetime([r['startTime'] for r in raw],utc=True)).sort_index()
            day=s.tz_convert(ec.CET).resample('D').mean();day.index=day.index.tz_localize(None)
            daily['FI']=day
            latest['FI']={'as_of':s.index[-1].tz_convert(ec.CET).date().isoformat(),'observed_at':s.index[-1].tz_convert(ec.CET).isoformat(),'wind_mw':float(s.iloc[-1]),
                         'source':'Fingrid dataset 268 installed wind capacity, CC BY 4.0','source_url':'https://data.fingrid.fi/en/datasets/268','status':'national observed time series'}
    file=CACHE/'dk_wind_register.xlsx'
    if not file.exists() or time.time()-file.stat().st_mtime>7*86400:
        def update_dk():
            import re
            from bs4 import BeautifulSoup
            page=requests.get('https://ens.dk/analyser-og-statistik/data-oversigt-over-energisektoren',timeout=45);page.raise_for_status()
            soup=BeautifulSoup(page.text,'html.parser')
            link=next(a['href'] for a in soup.find_all('a',href=True) if a.get_text(' ',strip=True).startswith('Vinddata (ultimo'))
            url='https://ens.dk'+link if link.startswith('/') else link
            r=requests.get(url,timeout=60);r.raise_for_status();file.write_bytes(r.content)
        safe('Danish capacity register refresh',update_dk)
    if file.exists():
        d=pd.read_excel(file,header=2)
        historical=CACHE/'dk_wind_historical.xlsx'
        if not historical.exists():
            def get_historical():
                r=requests.get('https://ens.dk/media/8746/download',timeout=60);r.raise_for_status();historical.write_bytes(r.content)
            safe('Danish historical capacity register',get_historical)
        production_columns=list(d.columns)
        if historical.exists():
            old=pd.read_excel(historical,header=2)
            d=pd.concat([old,d],ignore_index=True)
            idcol=next(c for c in d if str(c).startswith('Møllenummer'))
            d=d.drop_duplicates(idcol,keep='last')
        capcol=next(c for c in d if str(c).startswith('Kapacitet'))
        startcol=next(c for c in d if str(c).startswith('Dato for oprindelig'))
        endcol=next(c for c in d if str(c).startswith('Dato for afm'))
        cap=pd.to_numeric(d[capcol].astype(str).str.replace(',','.'),errors='coerce')/1000
        starts=pd.to_datetime(d[startcol],errors='coerce');ends=pd.to_datetime(d[endcol],errors='coerce')
        # File's own monthly production headings determine its coverage endpoint.
        months=[pd.Period(str(c),'M') for c in production_columns if len(str(c))==7 and str(c)[4]=='-' and str(c)[:4].isdigit()]
        asof=max(months).end_time.normalize()
        dates=pd.date_range('2015-01-01',pd.Timestamp(TODAY))
        vals=[cap[(starts<=min(t,asof)) & (ends.isna()|(ends>min(t,asof)))].sum(min_count=1) for t in dates]
        daily['DK']=pd.Series(vals,index=dates)
        latest['DK']={'as_of':str(asof.date()),'wind_mw':vals[-1],'source':'Danish Energy Agency public wind-turbine register',
                     'source_url':'https://ens.dk/media/8748/download','historical_source_url':'https://ens.dk/media/8746/download','historical_method':'current plus retired turbine registers, dates of connection/disconnection, reconstructed daily since 2015',
                     'status':'national register; held after coverage end, explicitly provisional',
                     'stale_days':max(0,(TODAY-asof.date()).days),'excluded_unknown_capacity':int(cap.isna().sum())}
    return latest,daily,zones


def generation():
    years=range(2015,TODAY.year+1);jobs=[(c,y) for c in COUNTRIES for y in years]
    gens={c:{} for c in COUNTRIES};caps={c:{} for c in COUNTRIES}
    def one(job):
        c,y=job
        a=dt.date(y,1,1);b=min(dt.date(y+1,1,1),TODAY)
        path=CACHE/f'gen_daily_v2_{c}_{y}.csv'
        if path.exists() and (y<TODAY.year or time.time()-path.stat().st_mtime<21600):
            frame=pd.read_csv(path,parse_dates=['date']).set_index('date')
        else:
            text=query(dict(documentType='A75',processType='A16',in_Domain=COUNTRIES[c],**ec._period(a,b)),f'gen_{c}_{y}',y<TODAY.year)
            items=ec.entsoe_parse(text)
            frame=pd.concat({k:daily_series(items,p) for k,p in {'wind':['B18','B19'],'hydro':['B11','B12'],'nuclear':['B14']}.items()},axis=1)
            frame.index.name='date';frame.to_csv(path)
        return c,y,frame
    for row in ec.pmap(lambda j:safe(f'generation {j[0]} {j[1]}',lambda:one(j)),jobs,6):
        if row:gens[row[0]][row[1]]=row[2]
    log('generation: annual capacity queries')
    for row in ec.pmap(lambda j:safe(f'capacity {j[0]} {j[1]}',lambda:(j[0],j[1],capacity(*j))),jobs,6):
        if row:caps[row[0]][row[1]]=row[2]
    out={'as_of':None,'source':'ENTSO-E A75 actual generation; A68 annual installed capacity',
         'history_requested_start':'2015-01-01','history_normal':'mean of same-calendar-day trailing 30-day values in all available preceding years; minimum 3 years',
         'columns':['date','value','ma30','normal_ma30','normal_n_years'],'units':{'wind_load_factor':'%','wind':'MW','hydro':'MW','nuclear':'MW'},'regions':{},
         'capacity':caps,'capacity_notes':'A68 annual values are not current turbine counts. Each year uses its own capacity, never latest capacity for historical output. Missing capacity gives null load factor. Sweden has no current-year country A68, so its last annual country A68 is held for current-year dates only (see capacity_latest.SE); this can slightly overstate the load factor if capacity grew. Bidding-zone A68 is a separate scope and is not summed to replace it.'}
    latest,daily_cap,zone_cap=capacity_supplement()
    out['capacity_latest']=latest
    out['capacity_zones']=zone_cap
    out['capacity_daily']={}
    frames={}
    denominators={}
    for c,yy in gens.items():
        if not yy:continue
        g=pd.concat(yy.values()).sort_index();g=g[~g.index.duplicated(keep='last')]
        for k in ['wind','hydro','nuclear']:
            if k not in g:g[k]=np.nan
        cap=pd.Series([caps[c].get(t.year,{}).get('wind_mw',np.nan) for t in g.index],index=g.index,dtype=float)
        if c in daily_cap:
            national=daily_cap[c].reindex(g.index)
            cap=national.combine_first(cap)
        held=None
        if c=='SE' and (caps[c].get(TODAY.year) or {}).get('wind_mw') is None:
            # No country A68 for the current year: hold the last annual country A68 for current-year dates only.
            # Earlier years keep their own capacity; nothing is applied backwards.
            prior=[y for y in caps[c] if y<TODAY.year and caps[c][y].get('wind_mw') is not None]
            if prior:
                held=caps[c][max(prior)]
                cur=g.index.year==TODAY.year
                cap[cur]=cap[cur].fillna(held['wind_mw'])
        denominators[c]=cap
        if c not in latest and held is not None:
            latest[c]=dict(held,status=f"annual ENTSO-E A68, {held['year']} vintage held for {TODAY.year} dates; not a current turbine count. ENTSO-E has no {TODAY.year} country A68 for Sweden; bidding-zone A68 not summed. If the fleet grew in {TODAY.year}, the load factor is slightly overstated",
                           applies_to=f'{TODAY.year}-01-01 onward',vintage_year=held['year'],stale_days=(TODAY-dt.date.fromisoformat(held['valid_through'])).days)
        if c not in latest:
            record=caps[c].get(TODAY.year)
            latest[c]=dict(record,status='annual ENTSO-E, not a current turbine census') if record else dict(caps[c].get(max(caps[c]),{}),status='last available annual observation only; not used for current load factor. National Swedish PxWeb failed TLS verification; not bypassed')
        out['capacity_daily'][c]={'columns':['date','wind_mw'],'rows':[[str(t.date()),v] for t,v in cap.items() if t>=pd.Timestamp(TODAY-dt.timedelta(days=122))],'latest_source':latest[c]}
        g['wind_load_factor']=100*g.wind/cap
        # Reporting/capacity mismatches are not silently clipped.
        g.loc[(g.wind_load_factor<0)|(g.wind_load_factor>110),'wind_load_factor']=np.nan
        frames[c]=g
    notes=safe('DK Energinet generation fill',lambda:fill_dk_wind(frames,denominators,daily_cap,latest,out))
    if notes:out.setdefault('source_notes',[]).extend(notes)
    out.setdefault('source_notes',[]).append('NO wind 2026-10-26 remains null: ENTSO-E A75 onshore (B19) is missing 14:00-14:45 UTC (4 of 96 intervals); not interpolated')
    out.setdefault('source_notes',[]).append('SE wind 2026-11-08 remains null: ENTSO-E A75 wind is missing 15:45-16:00 UTC (1 of 96 intervals); not interpolated, so SE and Nordic 30-day averages stay blank until that day leaves the window')
    if all(c in frames for c in COUNTRIES):
        g=pd.concat(frames.values()).sort_index()
        nord=pd.concat({k:pd.concat([frames[c][k] for c in members],axis=1).sum(axis=1,min_count=len(members)) for k,members in {'wind':list(COUNTRIES),'hydro':['NO','SE','FI'],'nuclear':['SE','FI']}.items()},axis=1)
        out['aggregate_members']={'wind':list(COUNTRIES),'hydro':['NO','SE','FI'],'nuclear':['SE','FI']}
        capacity_by_year={y:sum(caps[c][y]['wind_mw'] for c in COUNTRIES) for y in years if all(y in caps[c] and caps[c][y]['wind_mw'] is not None for c in COUNTRIES)}
        den=pd.concat(denominators.values(),axis=1).sum(axis=1,min_count=4)
        nord['wind_load_factor']=100*nord.wind/den.reindex(nord.index)
        frames['Nordic']=nord
    for c,g in frames.items():
        valid=g[['wind','hydro']].dropna(how='all')
        if valid.empty:continue
        reg={'as_of':str(valid.index.max().date()),'history_start':str(valid.index.min().date()),'metrics':{}}
        for k in ['wind_load_factor','hydro','nuclear']:
            s=g[k].reindex(pd.date_range(g.index.min(),g.index.max()));ma=trailing(s)
            hist=ma[ma.index.year<TODAY.year].dropna()
            groups=hist.groupby(hist.index.strftime('%m-%d'))
            norms=groups.mean();n=groups.count()
            dates=s.index[s.index>=pd.Timestamp(TODAY-dt.timedelta(days=122))]
            rows=[]
            for t in dates:
                md=t.strftime('%m-%d');ny=int(n.get(md,0));nv=norms.get(md,np.nan) if ny>=3 else np.nan
                rows.append([str(t.date()),s.loc[t],ma.loc[t],nv,ny])
            last=s.dropna().index.max() if s.notna().any() else None
            reg['metrics'][k]={'as_of':str(last.date()) if last is not None else None,'history_start':str(s.dropna().index.min().date()) if last is not None else None,'rows':rows}
        out['regions'][c]=reg
    out['as_of']=max((x['as_of'] for x in out['regions'].values()),default=None)
    umm=safe('nuclear UMM',lambda:bp.build_nuclear(dt.datetime.now(dt.timezone.utc),TODAY))
    if umm:
        for unit in umm['units']:
            for k in ['start','back']:
                unit[k]=pd.Timestamp(unit[k]).tz_convert(ec.CET).isoformat()
        out['nuclear_availability']={'as_of':str(TODAY),'observed_at':dt.datetime.now(ec.CET).isoformat(timespec='seconds'),'installed_mw':umm['cap'],'online_mw':umm['now'],'reactors_out':umm['units'],'source':umm['source'],'definition':'available capacity from active REMIT messages; not measured generation'}
    write('generation.json',out)
    return frames

def hydrology():
    base='https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/'
    data=get_json(base+'HentOffentligData',cache_key='nve_all',max_age=24*3600)
    band=get_json(base+'HentOffentligDataMinMaxMedian',cache_key='nve_band',max_age=7*86400)
    out={'as_of':None,'source':base,'reservoirs':{},'monthly_inflow':{},'omitted':[],
         'balance_definition':'Reservoir-only storage anomaly, not full hydrological balance (snow/soil/groundwater excluded).'}
    for typ,n,c in [('NO',0,'NO')]+[('EL',n,f'NO{n}') for n in range(1,6)]:
        d=[x for x in data if x['omrType']==typ and x['omrnr']==n];d.sort(key=lambda x:x['dato_Id'])
        if not d:continue
        hist=[x for x in band if x['omrType']==typ and x['omrnr']==n];bd={int(x['iso_uke']):x for x in hist}
        cur=d[-1];b=bd.get(cur['iso_uke']);normal=b['medianFyllingsGrad']*cur['kapasitet_TWh'] if b else None
        out['reservoirs'][c]={'as_of':cur['dato_Id'][:10],'history_start':min(x['dato_Id'][:10] for x in d),
          'unit':'% full','band_period':'NVE published 2006-2025 reference','columns':['date','fill_pct','storage_twh'],
          'rows':[[x['dato_Id'][:10],100*x['fyllingsgrad'],x['fylling_TWh']] for x in d if x['dato_Id'][:10]>=str(TODAY-dt.timedelta(days=400))],
          'band_columns':['iso_week','min','median','max'],
          'band':[[x['iso_uke'],100*x['minFyllingsgrad'],100*x['medianFyllingsGrad'],100*x['maxFyllingsgrad']] for x in sorted(hist,key=lambda x:x['iso_uke'])],
          'reservoir_balance_twh':{'value':cur['fylling_TWh'],'normal':normal,'difference':cur['fylling_TWh']-normal if normal is not None else None}}
    # ENTSO-E stored energy, historical band in TWh rather than pretending it is a filling percentage.
    for c in ['SE','FI']:
        parts=[]
        for y in range(2015,TODAY.year+1):
            def fetch(y=y):
                text=query(dict(documentType='A72',processType='A16',in_Domain=COUNTRIES[c],**ec._period(dt.date(y,1,1),min(dt.date(y+1,1,1),TODAY+dt.timedelta(days=1)))),f'reservoir_{c}_{y}',y<TODAY.year)
                return ec._combine(ec.entsoe_parse(text))
            s=safe(f'reservoir {c} {y}',fetch)
            if s is not None:parts.append(s)
        if not parts:continue
        s=pd.concat(parts).sort_index();s=s[~s.index.duplicated(keep='last')]/1e6
        f=pd.DataFrame({'date':[t.tz_convert(ec.CET).date() for t in s.index],'v':s.values})
        f['year']=[d.isocalendar().year for d in f.date];f['week']=[d.isocalendar().week for d in f.date]
        f=f.groupby(['year','week'],as_index=False).last();h=f[f.year<TODAY.year];h=h.groupby('week').v.agg(['min','median','max','count'])
        last=f.iloc[-1];normal=h.loc[last.week,'median'] if last.week in h.index else None
        out['reservoirs'][c]={'as_of':str(last.date),'history_start':str(f.date.min()),'unit':'TWh stored (capacity % unavailable)',
          'columns':['date','storage_twh'],'rows':[[str(r.date),r.v] for r in f.itertuples() if r.date>=TODAY-dt.timedelta(days=400)],
          'band_columns':['iso_week','min','median','max','n_years'],'band':[[int(w),r['min'],r['median'],r['max'],int(r['count'])] for w,r in h.iterrows()],
          'band_period':f'2015-{TODAY.year-1}, actual available years per ISO week',
          'reservoir_balance_twh':{'value':last.v,'normal':normal,'difference':last.v-normal if normal is not None else None},'source':'ENTSO-E A72'}
    # Preserve the existing monthly energy-inflow estimator and its stated limitations.
    old=json.loads(Path('/workspace/emmi-pulse-auto/data/nordic_monthly.json').read_text())
    for c,values in old.get('inflow',{}).items():
        s=pd.Series(values,index=pd.PeriodIndex(old['months'],freq='M'),dtype=float)
        b=s[(s.index.year>=old['avg_years'][0])&(s.index.year<=old['avg_years'][1])]
        nm=b.groupby(b.index.month).mean();cnt=b.groupby(b.index.month).count()
        nonnull=s.dropna()
        out['monthly_inflow'][c]={'as_of':str(nonnull.index.max().end_time.date()) if len(nonnull) else None,
           'history_start':str(nonnull.index.min()) if len(nonnull) else None,'unit':'TWh/month','baseline':old['avg_years'],
           'columns':['month','value','normal','n_years'],'rows':[[str(m),v,nm.get(m.month),int(cnt.get(m.month,0))] for m,v in s.items()],
           'method':'Derived energy inflow = monthly change in reservoir energy plus hydro generation; interpolated weekly reservoir readings; not measured natural inflow',
           'stale_snapshot':old.get('generated_utc'),'source':'existing nordic_monthly.json; ENTSO-E A75/A72 and NVE'}
    out['omitted']=['Full water+snow hydro balance: no validated free basin-energy series integrated; reservoir-only anomaly is labelled separately.',
                    'Snowpack vs normal: free gridded seNorge/SMHI data exist, but no validated Nordic basin weighting and energy conversion in the existing pipeline; not fabricated.',
                    'DK reservoirs: not meaningful at Nordic hydro scale. SE/FI fill %: no matched capacity denominator; stored TWh shown instead.']
    out['as_of']=max((x['as_of'] for x in out['reservoirs'].values()),default=None)
    write('hydrology.json',out)

def tomorrow(price_series,normals,meta):
    target=TODAY+dt.timedelta(days=1);a,b=ec._cet_bounds(target,target+dt.timedelta(days=1));idx=pd.date_range(a,b,freq='h',inclusive='left')
    out={'as_of':str(TODAY),'delivery_date':str(target),'timezone':'Europe/Zurich','hours':[t.tz_convert(ec.CET).isoformat() for t in idx],
         'units':{'price':'EUR/MWh','wind_fc':'MW','solar_fc':'MW','load_fc':'MW','residual_load':'MW','temp':'degC','wind100':'m/s at 100m'},
         'source':'ENTSO-E A44 prices and A65 load; ENTSO-E A69 A01 day-ahead wind/solar, or A18 intradaily when A01 is a zero stub or unpublished; Fingrid and Energinet TSO forecasts as labelled fallbacks; Nord Pool official SYS hourly; Open-Meteo IFS temperature and 100m wind speed. MW is never invented from wind speed.',
         'regions':{},'temperature':{},'wind100':{}}
    n=len(idx)
    def one(item):
        z,e=item;res={'as_of':str(TODAY)};sources={}
        s=price_series.get(z,(None,None))[0]
        res['price']=none_list(s.resample('1h').mean().reindex(idx).tolist()) if s is not None else [None]*n
        sources['price']='ENTSO-E A44 day-ahead'
        parsed,src=entsoe_wind_solar('forecast '+z+' A01',e,'A01',target,idx,f'forecast_A01_{z}_{TODAY}')
        res.update(parsed);sources['wind_fc']=src if any(v is not None for v in parsed['wind_fc']) else None
        sources['solar_fc']=src if any(v is not None for v in parsed['solar_fc']) else None
        if all(v is None for v in res['wind_fc']) or all(v is None for v in res['solar_fc']):
            parsed,src=entsoe_wind_solar('forecast '+z+' A18',e,'A18',target,idx,f'forecast_A18_{z}_{TODAY}')
            fill_if_missing(res,parsed,sources,'wind_fc',src)
            fill_if_missing(res,parsed,sources,'solar_fc',src)
        text=safe('load forecast '+z,lambda:query(dict(documentType='A65',processType='A01',outBiddingZone_Domain=e,**ec._period(target,target+dt.timedelta(days=1))),f'loadfc_{z}_{TODAY}'))
        res['load_fc']=hourly_idx(ec.entsoe_parse(text),idx) if text else [None]*n
        sources['load_fc']='ENTSO-E A65 A01 day-ahead' if any(v is not None for v in res['load_fc']) else None
        res['forecast_source']=sources
        return z,res
    for row in ec.pmap(lambda j:safe('tomorrow '+j[0],lambda:one(j)),list(ZONES.items()),6):
        if row:out['regions'][row[0]]=row[1]
    # Finland: Fingrid official MW if ENTSO-E still lacks a real series.
    if 'FI' in out['regions'] and (all(v is None for v in out['regions']['FI']['wind_fc']) or all(v is None for v in out['regions']['FI']['solar_fc'])):
        fg=safe('Fingrid FI forecast',lambda:fingrid_forecast(idx))
        if fg:
            src='Fingrid open data (CC BY 4.0), latest 15-minute forecast (datasets 245/248); ENTSO-E A69 A01 not published for this delivery day'
            fill_if_missing(out['regions']['FI'],fg,out['regions']['FI']['forecast_source'],'wind_fc',src)
            fill_if_missing(out['regions']['FI'],fg,out['regions']['FI']['forecast_source'],'solar_fc',src)
    # Denmark: Energinet day-ahead if ENTSO-E lacks tomorrow.
    if any(z in out['regions'] and all(v is None for v in out['regions'][z]['wind_fc']) for z in ['DK1','DK2']):
        eds=safe('Energinet DK forecast',lambda:energinet_forecast(idx,target)) or {}
        src='Energinet Energi Data Service Forecasts_Hour, ForecastDayAhead'
        for z,parsed in eds.items():
            if z in out['regions']:
                fill_if_missing(out['regions'][z],parsed,out['regions'][z]['forecast_source'],'wind_fc',src)
                fill_if_missing(out['regions'][z],parsed,out['regions'][z]['forecast_source'],'solar_fc',src)
    groups={'SE':['SE1','SE2','SE3','SE4'],'NO':['NO1','NO2','NO3','NO4','NO5'],'DK':['DK1','DK2']}
    for c,zz in groups.items():
        if not all(z in out['regions'] for z in zz):continue
        res={'as_of':str(TODAY),'price_definition':'equal-weight zone average, not load-weighted','forecast_source':{}}
        for k in ['price','wind_fc','solar_fc','load_fc']:
            f=pd.DataFrame([out['regions'][z][k] for z in zz]);s=f.sum(axis=0,min_count=len(zz))
            res[k]=none_list((s/len(zz) if k=='price' else s).tolist())
            if k=='price':res['forecast_source'][k]='equal-weight zone average of ENTSO-E A44'
            elif all(v is not None for v in res[k]):
                res['forecast_source'][k]='sum of bidding-zone forecasts'
        # Official country-level ENTSO-E series when a zone is missing (e.g. NO5 A18 wind stub).
        if all(v is None for v in res['wind_fc']) or all(v is None for v in res['solar_fc']):
            for proc in ['A01','A18']:
                parsed,src=entsoe_wind_solar(f'forecast {c} country {proc}',COUNTRIES[c],proc,target,idx,f'forecast_{proc}_{c}_{TODAY}')
                fill_if_missing(res,parsed,res['forecast_source'],'wind_fc',src+' country aggregate')
                fill_if_missing(res,parsed,res['forecast_source'],'solar_fc',src+' country aggregate')
                if any(v is not None for v in res['wind_fc']) and any(v is not None for v in res['solar_fc']):break
        out['regions'][c]=res
    # Official Nord Pool system price for the Nordic view; never a country average.
    nord={'as_of':str(TODAY),'price_definition':'official unconstrained Nordic system price, not a country average','forecast_source':{}}
    js=safe('Nord Pool SYS hourly',lambda:get_json(bp.NP_API+'DayAheadPrices',params=dict(date=str(target),market='DayAhead',deliveryArea='SYS',currency='EUR'),cache_key=f'np_sys_{target}',max_age=3600))
    if js:
        ent=[(e['deliveryStart'],(e.get('entryPerArea') or {}).get('SYS')) for e in js.get('multiAreaEntries') or []]
        ent=[(t,v) for t,v in ent if v is not None]
        if ent:
            s=pd.Series([v for _,v in ent],index=pd.to_datetime([t for t,_ in ent],utc=True),dtype=float).sort_index()
            nord['price']=none_list(s.resample('1h').mean().reindex(idx).tolist())
            nord['forecast_source']['price']='Nord Pool DayAheadPrices, official SYS'
        else:nord['price']=[None]*n
    else:nord['price']=[None]*n
    members=['NO','SE','FI','DK']
    if all(c in out['regions'] for c in members):
        for k in ['wind_fc','solar_fc','load_fc']:
            f=pd.DataFrame([out['regions'][c][k] for c in members])
            nord[k]=none_list(f.sum(axis=0,min_count=len(members)).tolist())
            if all(pd.notna(v) for v in nord[k]):nord['forecast_source'][k]='sum of country forecasts (NO+SE+FI+DK)'
    else:
        for k in ['wind_fc','solar_fc','load_fc']:nord[k]=[None]*n
    out['regions']['Nordic']=nord
    for code,res in out['regions'].items():
        res['residual_load']=[l-w-s if all(pd.notna(v) for v in [l,w,s]) else None for l,w,s in zip(res['load_fc'],res['wind_fc'],res['solar_fc'])]
        res['complete']={k:all(pd.notna(v) for v in res[k]) for k in ['price','wind_fc','solar_fc','load_fc','residual_load']}
    # Temperature and 100m wind speed use the same fractional-country grid weights as historical normals.
    pts=sorted({(x,y) for c in COUNTRIES for x,y,w in meta['weights'][c]})
    js=safe('weather forecast',lambda:get_json('https://api.open-meteo.com/v1/forecast',params=dict(latitude=','.join(str(p[0]) for p in pts),longitude=','.join(str(p[1]) for p in pts),hourly='temperature_2m,wind_speed_100m',timezone='GMT',start_date=str(a.date()),end_date=str(b.date()),models='ecmwf_ifs025',wind_speed_unit='ms'),cache_key=f'metfc_utc_{TODAY}',max_age=21600))
    if js:
        if not isinstance(js,list):js=[js]
        if len(js)!=len(pts):raise ValueError('weather forecast point mismatch')
        def arr(key):
            arrays={p:pd.Series(j['hourly'][key],index=pd.to_datetime(j['hourly']['time'],utc=True),dtype=float).reindex(idx).to_numpy() for p,j in zip(pts,js)}
            if any(np.isnan(v).any() or len(v)!=len(idx) for v in arrays.values()):raise ValueError(f'Incomplete {key} forecast delivery hours')
            return arrays
        temps,winds=arr('temperature_2m'),arr('wind_speed_100m')
        for c in REGIONS:
            weights=meta['weights'][c];wt=[w for x,y,w in weights]
            tv=np.average(np.stack([temps[(x,y)] for x,y,w in weights]),axis=0,weights=wt)
            wv=np.average(np.stack([winds[(x,y)] for x,y,w in weights]),axis=0,weights=wt)
            tnorm=float(normals[c].loc[target.strftime('%m-%d'),'temp']);wnorm=float(normals[c].loc[target.strftime('%m-%d'),'wind100'])
            out['temperature'][c]={'as_of':str(TODAY),'hourly':tv.tolist(),'daily_mean':float(np.mean(tv)),'daily_normal':tnorm,'difference_degC':float(np.mean(tv))-tnorm,
             'normal_baseline':list(BASELINE),'note':'Forecast local delivery-day mean vs ERA5 UTC daily climatology; small timezone alignment difference, not hourly normals'}
            out['wind100'][c]={'as_of':str(TODAY),'hourly':wv.tolist(),'daily_mean':float(np.mean(wv)),'daily_normal':wnorm,
             'difference':float(np.mean(wv))-wnorm,'difference_pct':((float(np.mean(wv))/wnorm-1)*100 if wnorm else None),
             'normal_baseline':list(BASELINE),'source':'Open-Meteo IFS 100m wind speed at the same land-area-weighted points as ERA5 history; not converted to MW',
             'note':'Use this speed-vs-normal comparison when a MW wind forecast is unavailable. Never treat this as generation or residual load.'}
    write('tomorrow.json',out)

def monthly():
    # Exact legacy payloads, deliberately not refreshed or reinterpreted.
    names=['nordic_mtd','nordic_monthly']
    result={'as_of':None,'source':'existing EMMI monthly-view snapshots, reused unchanged','stale':True,'snapshots':{}}
    for n in names:
        j=json.loads(Path('/workspace/emmi-pulse-auto/data/'+n+'.json').read_text());result['snapshots'][n]=j
    result['climate_100m']=json.loads((CLIMATE/'mtd.json').read_text())
    result['wind_display_rule']='Only use climate_100m.wind100 for wind speed; legacy wx wind fields are not approved 100m and must not be rendered.'
    dates=result['snapshots']['nordic_mtd'].get('days',[])
    result['as_of']=max(dates) if dates else None
    result['note']='Legacy snapshot is not represented as current. Native fields and units preserved. Display numeric values to 1 decimal place; label dashed average with its numeric value. Monthly climate tiles should use monthly_climate.json (2006-2025, new geography), not climate_100m.'
    write('monthly.json',result)

def monthly_climate():
    """Monthly climate on the same raw 100m / country-mask geography as weather.json, 2006-2025 baseline."""
    d,meta=climate.regional_raw_daily(CACHE/'countries.geojson',CACHE/'regional_daily.csv')
    units={'wind100':'m/s at 100m','temp':'degC at 2m','rain':'mm/day'}
    result={'as_of':str(d.date.max().date()),'baseline':list(BASELINE),'history_start':'2021-01',
            'units':units,'source':'https://archive-api.open-meteo.com/v1/archive?models=era5',
            'method':meta['method'],'grid':meta['grid'],'point_counts':meta['points'],
            'aggregation':'mean of daily values in the month (or the available days if partial); rain stays mm/day, not a monthly total',
            'columns':['month','value','normal','min','max','difference','difference_pct','days_covered','days_in_month','partial'],
            'regions':{}}
    start=pd.Timestamp('2021-01-01')
    for c,g in d.groupby('region'):
        g=g.sort_values('date').set_index('date')
        b=g.loc[f'{BASELINE[0]}-01-01':f'{BASELINE[1]}-12-31']
        end=g[list(climate.u.VARS)].dropna(how='all').index.max()
        months=pd.period_range(start,end,freq='M')
        metrics={k:{'as_of':str(g[k].dropna().index.max().date()) if g[k].notna().any() else None,'rows':[]} for k in climate.u.VARS}
        for period in months:
            days=pd.date_range(period.start_time,period.end_time,freq='D')
            days=days[days<=end]
            days_in_month=period.days_in_month
            days_covered=int(g.loc[days,list(climate.u.VARS)].dropna(how='all').shape[0]) if len(days) else 0
            # Require every requested calendar day for the metric; a gap is not a shorter month.
            partial=len(days)<days_in_month
            for k in climate.u.VARS:
                cur=g.reindex(days)[k]
                if cur.notna().sum()!=len(days) or not len(days):
                    value=None
                else:
                    value=float(cur.mean())
                yearly=[]
                for year in range(BASELINE[0],BASELINE[1]+1):
                    use=[]
                    for t in days:
                        try:use.append(t.replace(year=year))
                        except ValueError:continue
                    hs=g.reindex(use)[k]
                    if hs.notna().sum()==len(use) and use:yearly.append(float(hs.mean()))
                normal=float(np.mean(yearly)) if yearly else None
                mn=float(np.min(yearly)) if yearly else None
                mx=float(np.max(yearly)) if yearly else None
                diff=(value-normal) if value is not None and normal is not None else None
                pct=((value/normal-1)*100 if k!='temp' and value is not None and normal not in (None,0) else None)
                metrics[k]['rows'].append([period.strftime('%Y-%m'),value,normal,mn,mx,diff,pct,int(cur.notna().sum()),days_in_month,partial])
        result['regions'][c]={'as_of':str(end.date()),'metrics':metrics}
    write('monthly_climate.json',result)

def main():
    global TODAY,BASELINE,OUT
    import fcntl
    lock=(BASE/'build.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Another Nordic v2 builder is already running; no concurrent cache/output writes allowed')
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',default='2006:2025');ap.add_argument('--out',type=Path,default=OUT);ap.add_argument('--only',default='all');args=ap.parse_args()
    BASELINE=tuple(map(int,args.baseline.split(':')));OUT=args.out;OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    if len(BASELINE)!=2 or BASELINE[0]>BASELINE[1] or BASELINE[1]>=TODAY.year:raise ValueError('baseline must contain two completed years')
    log(f'Nordic v2 build {TODAY} Europe/Zurich; baseline {BASELINE}')
    selected=lambda s:args.only=='all' or s in args.only.split(',')
    normals=meta=None;px=None
    if selected('weather'):normals,meta=weather()
    if selected('prices'):px=prices()
    if selected('generation'):generation()
    if selected('hydrology'):safe('hydrology',hydrology)
    if selected('tomorrow'):
        if normals is None:normals,meta=weather()
        if px is None:px=prices()
        tomorrow(px,normals,meta)
    if selected('monthly'):monthly()
    if selected('monthly_climate'):monthly_climate()
    files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'as_of':json.loads(p.read_text()).get('as_of')} for p in OUT.glob('*.json') if p.name!='manifest.json'}
    write('manifest.json',{'as_of':str(TODAY),'built_at':dt.datetime.now(ec.CET).isoformat(timespec='seconds'),'baseline':list(BASELINE),'files':files,'errors':ERRORS,
                         'status':'partial' if ERRORS else 'built','policy':'null means unavailable; no invented, silently carried-forward or paid inputs'})
    log(f'Done. {len(ERRORS)} source gaps; see manifest.json')
if __name__=='__main__':main()
