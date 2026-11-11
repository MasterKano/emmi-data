#!/workspace/brief/.venv/bin/python
"""Verify the actual JSON contract and source values, independently of UI rendering."""
import datetime as dt, hashlib, json, math, sys, unittest
from pathlib import Path
import numpy as np
import pandas as pd
from shapely.geometry import shape,box
import build
ROOT=Path(__file__).resolve().parent
OUT=Path(sys.argv[1]) if len(sys.argv)>1 and not sys.argv[1].startswith('-') else ROOT/'output'
if len(sys.argv)>1 and not sys.argv[1].startswith('-'):sys.argv.pop(1)
def read(n):return json.loads((OUT/(n+'.json')).read_text())

def manual_mean(df,country,var):
    codes={'SE':'SWE','NO':'NOR','FI':'FIN','DK':'DNK'}
    js=json.loads((ROOT/'cache/countries.geojson').read_text())
    geoms=[shape(f['geometry']) for f in js['features'] if f['properties']['ADM0_A3'] in (list(codes.values()) if country=='Nordic' else [codes[country]])]
    weights=[]
    for r in df.itertuples():
        lat=min(range(55,72,2),key=lambda a:abs(a-r.lat));lon=min(range(4,33,4),key=lambda a:abs(a-r.lon))
        area=sum(box(lon-2,lat-1,lon+2,lat+1).intersection(g).area for g in geoms)
        weights.append(area*math.cos(math.radians(lat)))
    return np.average(df[var],weights=weights)

class Contract(unittest.TestCase):
    def test_manifest_matches_actual_files(self):
        for name,meta in read('manifest')['files'].items():
            content=(OUT/name).read_bytes();self.assertEqual(len(content),meta['bytes']);self.assertEqual(hashlib.sha256(content).hexdigest(),meta['sha256'])
    def test_json_sizes_dates_and_finite_numbers(self):
        total=0
        for p in OUT.glob('*.json'):
            j=json.loads(p.read_text(),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
            self.assertEqual(j['schema_version'],1)
            if j['as_of']:dt.date.fromisoformat(j['as_of'])
            self.assertLess(p.stat().st_size,200000);total+=p.stat().st_size
        self.assertLess(total,650000)  # room for prices_normals after the 2021-2025 price backfill
    def test_20_year_weather_baseline_and_all_regions(self):
        w=read('weather');self.assertEqual(w['baseline'],[2006,2025]);self.assertEqual(w['history_start'],'1940-01-01')
        self.assertEqual(set(w['regions']),{'Nordic','SE','NO','FI','DK'})
        self.assertEqual(w['units']['wind100'],'m/s at 100m')
        for c,r in w['regions'].items():
            self.assertGreaterEqual(w['point_counts'][c],5)
            for k,m in r['metrics'].items():
                rows=m['rows'];self.assertEqual(len(rows),123)
                self.assertEqual(len(set(x[0] for x in rows)),123)
                self.assertAlmostEqual(sum(x[1] for x in rows[-30:])/30,rows[-1][2],delta=.001)
                self.assertAlmostEqual(sum(x[3] for x in rows[-30:])/30,rows[-1][4],delta=.001)
                self.assertAlmostEqual(m['summary']['difference'],m['summary']['value']-m['summary']['normal'],delta=.002)
                self.assertEqual(m['summary']['difference_unit'],w['units'][k])
    def test_weather_source_height_and_country_weights(self):
        w=read('weather');end=dt.date.fromisoformat(w['as_of']);day=end.replace(day=1)-dt.timedelta(days=1)
        df=pd.read_csv(f'/home/box/data/nordic-climate/cache/openmeteo100m/om_{day.year}_{day.month:02d}.csv')
        df=df[df.date==str(day)]
        for c in ['Nordic','SE','NO','FI','DK']:
            expected=manual_mean(df,c,'wind100')
            actual=next(x[1] for x in w['regions'][c]['metrics']['wind100']['rows'] if x[0]==str(day))
            self.assertAlmostEqual(expected,actual,delta=.0006)
    def test_normal_uses_exact_2006_2025_source_days(self):
        vals=[]
        for year in range(2006,2026):
            df=pd.read_csv(f'/home/box/data/nordic-climate/cache/openmeteo100m/om_{year}_11.csv')
            vals.append(manual_mean(df[df.date==f'{year}-11-01'],'DK','wind100'))
        normal=next(r[1] for r in read('weather_normals')['regions']['DK'] if r[0]=='11-01')
        self.assertAlmostEqual(sum(vals)/20,normal,delta=.0006)
    def test_price_30_calendar_days(self):
        for c,r in read('prices')['regions'].items():
            rows=r['rows'];s=pd.Series({pd.Timestamp(x[0]):x[1] for x in rows})
            for day,value,ma in rows[29:]:
                days=pd.date_range(pd.Timestamp(day)-pd.Timedelta(days=29),pd.Timestamp(day))
                v=s.reindex(days)
                if v.notna().all():self.assertAlmostEqual(v.mean(),ma,delta=.001)
                else:self.assertIsNone(ma)
        f=read('prices')['futures'];self.assertIn('front_month',f);self.assertIn('next_quarter',f)
    def test_price_baseline_2021_2025_and_unbroken_rows(self):
        p=read('prices');n=read('prices_normals')
        self.assertEqual(p['columns'],['date','value','ma30'])
        self.assertEqual(p['baseline'],[2021,2025])
        self.assertEqual(p['baseline_label'],'2021–2025')
        self.assertEqual(p['history_start'],'2021-01-01')
        self.assertEqual(n['baseline'],[2021,2025])
        self.assertEqual(n['baseline_label'],'2021–2025')
        self.assertEqual(len(n['month_days']),366)
        self.assertIn('02-29',n['month_days'])
        zones=['NO1','NO2','NO3','NO4','NO5','SE1','SE2','SE3','SE4','FI','DK1','DK2']
        for c in zones+['SE','NO','DK']:
            r=p['regions'][c]
            self.assertEqual(r['history_start'],'2021-01-01')
            self.assertEqual(r['baseline_label'],'2021–2025')
            self.assertEqual(r['coverage']['gap_count'],0)
            self.assertEqual(r['coverage']['gaps'],[])
            rows=r['rows']
            self.assertEqual(len(rows),123)
            self.assertEqual(len(set(x[0] for x in rows)),123)
            self.assertEqual([len(x) for x in rows],[3]*123)
        nord=p['regions']['Nordic']
        self.assertEqual(nord['history_start'],'2024-01-01')
        self.assertEqual(nord['baseline_label'],'2024–2025')
        self.assertEqual(nord['coverage']['gap_count'],0)
        # Seasonal normal is the mean of the same month/day in 2021-2025, from the cached daily files.
        parts=[]
        for f in sorted((ROOT/'cache').glob('price_daily_SE3_*.csv')):
            s=pd.read_csv(f,parse_dates=['date'],index_col='date')['value']
            s.index=pd.to_datetime(s.index);parts.append(s)
        s=pd.concat(parts).sort_index();s=s[~s.index.duplicated(keep='last')]
        vals=[float(s.loc[pd.Timestamp(f'{y}-01-01')]) for y in range(2021,2026)]
        self.assertAlmostEqual(sum(vals)/5,n['normal']['SE3'][n['month_days'].index('01-01')],delta=.002)
    def test_generation_capacity_date_and_real_division(self):
        g=read('generation')
        fi=json.loads((ROOT/f'cache/fi_capacity_{build.TODAY.year}.json').read_text())
        self.assertEqual(g['capacity_latest']['FI']['as_of'],pd.Timestamp(fi[-1]['startTime']).tz_convert(build.ec.CET).date().isoformat())
        self.assertLessEqual(g['capacity_latest']['DK']['as_of'],str(build.TODAY))
        self.assertEqual(g['capacity_latest']['NO']['as_of'],f'{build.TODAY.year}-01-01')
        for c in ['NO','FI','DK']:
            cap=dict(g['capacity_daily'][c]['rows'])
            rows=g['regions'][c]['metrics']['wind_load_factor']['rows']
            good=[r for r in rows if r[1] is not None]
            self.assertTrue(good)
            d,value,*_=good[-1]
            raw=pd.read_csv(ROOT/f'cache/gen_daily_v2_{c}_{d[:4]}.csv',parse_dates=['date']).set_index('date');raw.index=raw.index.strftime('%Y-%m-%d')
            self.assertAlmostEqual(100*raw.loc[d,'wind']/cap[d],value,delta=.001)
        if str(build.TODAY.year) not in g['capacity']['SE']:
            self.assertTrue(all(r[1] is None for r in g['regions']['SE']['metrics']['wind_load_factor']['rows'] if r[0][:4]==str(build.TODAY.year)))
        self.assertEqual(g['aggregate_members']['nuclear'],['SE','FI'])
        self.assertIsNotNone(g['regions']['Nordic']['metrics']['hydro']['rows'][-1][1])
        self.assertIsNotNone(g['regions']['Nordic']['metrics']['nuclear']['rows'][-1][1])
    def test_delivery_days_dst_and_incomplete_input(self):
        for day,hours in [(dt.date(2026,3,29),23),(dt.date(2026,10,25),25)]:
            a,b=build.ec._cet_bounds(day,day+dt.timedelta(days=1));idx=pd.date_range(a,b,freq='h',inclusive='left')
            self.assertEqual(len(idx),hours)
            item={'is_in':True,'psr':'B19','step':60,'s':pd.Series(1000.,index=idx)}
            result=build.daily_series([item],['B19']);self.assertEqual(result.loc[pd.Timestamp(day)],1000.)
            item['s']=item['s'].drop(idx[2]);self.assertTrue(build.daily_series([item],['B19']).empty)
    def test_tomorrow_residual_and_missing_not_zero(self):
        t=read('tomorrow');self.assertEqual(t['delivery_date'],str(build.TODAY+dt.timedelta(days=1)))
        for c,r in t['regions'].items():
            for k in ['price','wind_fc','solar_fc','load_fc','residual_load']:self.assertEqual(len(r[k]),len(t['hours']))
            for p,w,s,l,res in zip(r['price'],r['wind_fc'],r['solar_fc'],r['load_fc'],r['residual_load']):
                if None in [w,s,l]:self.assertIsNone(res)
                else:self.assertAlmostEqual(l-w-s,res,delta=.003)
        for c,temp in t['temperature'].items():self.assertEqual(len(temp['hourly']),len(t['hours']))
        # Residual is only complete when wind, solar and load all exist. Norway solar is an A03 zero stub, so residual stays null.
        if not t['regions']['NO']['complete']['solar_fc']:
            self.assertFalse(t['regions']['NO']['complete']['residual_load'])
            self.assertTrue(all(x is None for x in t['regions']['NO']['residual_load']))
        if not t['regions']['SE']['complete']['wind_fc']:
            self.assertTrue(any(x is None for x in t['regions']['SE']['residual_load']))
        no=t['regions']['NO']['wind_fc']
        self.assertTrue(any(v not in (None,0,0.0) for v in no), 'Norway wind forecast must not be the A01 zero stub')
        self.assertTrue(t['regions']['NO']['complete']['wind_fc'])
        self.assertIn('Nordic',t['regions'])
        self.assertTrue(any(v is not None for v in t['regions']['Nordic']['price']))
        self.assertEqual(set(t['wind100']),{'Nordic','SE','NO','FI','DK'})
        self.assertEqual(len(t['wind100']['Nordic']['hourly']),len(t['hours']))
    def test_legacy_snapshots_unchanged_and_height_guard(self):
        m=read('monthly')
        for name,j in m['snapshots'].items():
            self.assertEqual(j,json.loads(Path('/workspace/emmi-pulse-auto/data',name+'.json').read_text()))
        self.assertEqual(m['climate_100m']['wind_height_m'],100)
        self.assertIn('must not be rendered',m['wind_display_rule'])
    def test_hydrology_units_and_omissions_are_honest(self):
        h=read('hydrology');self.assertEqual(h['reservoirs']['NO']['history_start'],'1995-01-08')
        self.assertEqual(h['reservoirs']['NO']['unit'],'% full')
        self.assertIn('TWh',h['reservoirs']['SE']['unit'])
        self.assertIn('not full',h['balance_definition'])
        self.assertTrue(any('Snowpack' in x for x in h['omitted']))
        self.assertEqual(h['monthly_inflow']['NO']['as_of'],'2026-09-30')
    def test_monthly_climate_20yr_new_geography(self):
        m=read('monthly_climate')
        self.assertEqual(m['baseline'],[2006,2025])
        self.assertEqual(m['history_start'],'2021-01')
        self.assertEqual(set(m['regions']),{'Nordic','SE','NO','FI','DK'})
        self.assertEqual(m['units']['wind100'],'m/s at 100m')
        cols=m['columns']
        self.assertEqual(cols[0],'month')
        for c,r in m['regions'].items():
            wind=r['metrics']['wind100']['rows']
            self.assertGreaterEqual(len(wind),12)
            self.assertEqual(wind[0][0],'2021-01')
            last=wind[-1]
            row={cols[i]:last[i] for i in range(len(cols))}
            self.assertTrue(row['partial'])
            self.assertGreater(row['days_covered'],0)
            self.assertLess(row['days_covered'],row['days_in_month'])
            if row['value'] is not None and row['normal'] is not None:
                self.assertAlmostEqual(row['difference'],row['value']-row['normal'],delta=.002)
            jan=next(x for x in wind if x[0]=='2021-01')
            self.assertFalse(jan[cols.index('partial')])
            temp=r['metrics']['temp']['rows'][-1]
            self.assertIsNone(temp[cols.index('difference_pct')])

if __name__=='__main__':unittest.main(verbosity=2)
