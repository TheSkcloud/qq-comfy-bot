"""One persisted weather attempt per local date, including failures and restarts."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import time
import json
import os
from pathlib import Path
import sqlite3
import urllib.request

import aiohttp

CODES = {0:'晴',1:'大部晴朗',2:'多云',3:'阴',45:'雾',48:'雾凇',51:'小毛毛雨',53:'毛毛雨',55:'较强毛毛雨',
         61:'小雨',63:'中雨',65:'大雨',71:'小雪',73:'中雪',75:'大雪',80:'阵雨',81:'阵雨',82:'强阵雨',95:'雷雨',96:'雷雨伴冰雹',99:'雷雨伴冰雹'}


class DailyWeather:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True, parents=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, status TEXT, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (day TEXT PRIMARY KEY, count INTEGER NOT NULL, lease REAL NOT NULL)')
        self.lock=asyncio.Lock()

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read(self, day):
        with self.db() as db:
            row = db.execute('SELECT status,payload FROM daily WHERE day=?', (day,)).fetchone()
        return dict(status=row[0], **json.loads(row[1])) if row else None

    async def ensure(self, http, current):
        day = current.date().isoformat()
        async with self.lock:
            while True:
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT status,payload FROM daily WHERE day=?',(day,)).fetchone()
                    if row and row[0]=='ok':
                        return dict(status=row[0],**json.loads(row[1]))
                    state=db.execute('SELECT count,lease FROM attempts WHERE day=?',(day,)).fetchone()
                    # Existing pre-upgrade failed or pending rows count as one attempt.
                    count,lease=state if state else (1 if row else 0,0)
                    if lease and time.time()-lease<60:
                        return self.read(day)
                    if count>=5:
                        payload={'date':day,'location':'本地','summary':'今日天气未取得，五次尝试均失败；不会使用旧天气。','attempts':count}
                        db.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?)',(day,'unavailable',json.dumps(payload,ensure_ascii=False)))
                        return dict(status='unavailable',**payload)
                    count+=1
                    db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)',(day,count,time.time()))
                    payload={'date':day,'location':'本地','summary':'今日天气正在同步','attempts':count}
                    db.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?)',(day,'pending',json.dumps(payload,ensure_ascii=False)))
                try:
                    payload=await self.fetch(http,current,'open-meteo' if count in (1,3,5) else 'met-no')
                    payload['attempts']=count
                    status='ok'
                except Exception as exc:
                    payload={'date':day,'location':'本地','summary':'今日天气暂时不可用','error':type(exc).__name__,'attempts':count}
                    status='unavailable'
                with self.db() as db:
                    db.execute('UPDATE attempts SET lease=0 WHERE day=?',(day,))
                    db.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?)',(day,status,json.dumps(payload,ensure_ascii=False)))
                if status=='ok':
                    return dict(status=status,**payload)

    async def fetch(self,http,current,source):
        latitude = float(os.getenv("WEATHER_LATITUDE", "39.90"))
        longitude = float(os.getenv("WEATHER_LONGITUDE", "116.40"))
        day=current.date().isoformat()
        proxy=urllib.request.getproxies().get('https')
        if source=='open-meteo':
            params=dict(latitude=latitude,longitude=longitude,timezone='Asia/Shanghai',daily='weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max',start_date=day,end_date=day)
            async with http.get('https://api.open-meteo.com/v1/forecast',params=params,proxy=proxy,timeout=aiohttp.ClientTimeout(total=18)) as response:
                response.raise_for_status(); raw=await response.json()
            daily=raw['daily']
            if daily['time'][0]!=day: raise ValueError('Weather date mismatch')
            values=[daily[k][0] for k in ('weather_code','temperature_2m_min','temperature_2m_max','precipitation_probability_max','wind_speed_10m_max')]
            if any(v is None or not isinstance(v,(int,float)) for v in values): raise ValueError('Incomplete forecast')
            code,low,high,rain,wind=values
            payload=dict(date=day,location='本地',source='Open-Meteo',condition=CODES.get(code,'天气变化'),low=low,high=high,rain_probability=rain,wind_kmh=wind)
            payload['summary']=f'本地今日预报：{payload["condition"]}，{low}–{high}℃，降水概率{rain}%，最大风速{wind}km/h。'
            return payload
        async with http.get('https://api.met.no/weatherapi/locationforecast/2.0/compact',params={'lat':latitude,'lon':longitude},proxy=proxy,headers={'User-Agent':'QQCompanion/1.0 (local personal weather)'},timeout=aiohttp.ClientTimeout(total=18)) as response:
            response.raise_for_status();raw=await response.json()
        series=[]
        for item in raw['properties']['timeseries']:
            dt=datetime.fromisoformat(item['time'].replace('Z','+00:00')).astimezone(current.tzinfo)
            if dt.date()==current.date():series.append(item)
        if not series:raise ValueError('No remaining forecast for today')
        temps=[x['data']['instant']['details']['air_temperature'] for x in series]
        winds=[x['data']['instant']['details'].get('wind_speed',0)*3.6 for x in series]
        symbols=[x['data'].get('next_1_hours',x['data'].get('next_6_hours',{})).get('summary',{}).get('symbol_code','') for x in series]
        symbols=[x for x in symbols if x]
        if not symbols:raise ValueError('Missing forecast conditions')
        symbol=symbols[0]
        condition='有雨或雪' if any(x in symbol for x in ('rain','snow','sleet')) else ('多云或阴' if 'cloud' in symbol else '晴或少云')
        low,high=round(min(temps),1),round(max(temps),1)
        payload=dict(date=day,location='本地',source='MET Norway',condition=condition,low=low,high=high,rain_probability=None,wind_kmh=round(max(winds),1))
        payload['summary']=f'本地今日剩余时段预报：{condition}，约{low}–{high}℃，降水概率未提供。'
        return payload

    async def loop(self, http, clock):
        while True:
            await self.ensure(http, clock())
            await asyncio.sleep(1800)
