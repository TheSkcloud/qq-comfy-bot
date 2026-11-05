import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiohttp import web, ClientSession
from context_engine import ContextEngine
from life import Life,redact
import schedule
from weather import DailyWeather
from web_manager import Dashboard

NOW=datetime(2026,10,28,14,tzinfo=ZoneInfo('Asia/Shanghai'))


class Response:
    def __init__(self,data=None,fail=False):self.data=data;self.fail=fail
    async def __aenter__(self):
        if self.fail:raise OSError('network unavailable')
        return self
    async def __aexit__(self,*args):pass
    def raise_for_status(self):pass
    async def json(self):return self.data


class WeatherHTTP:
    def __init__(self,fail=False):self.count=0;self.fail=fail
    def get(self,*args,**kwargs):
        self.count+=1
        data={'daily':{'time':[kwargs['params']['start_date']],'weather_code':[3], 'temperature_2m_max':[15], 'temperature_2m_min':[3], 'precipitation_probability_max':[20],'wind_speed_10m_max':[10]}}
        return Response(data,self.fail)


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_one_call_per_day_even_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'weather.db';http=WeatherHTTP();service=DailyWeather(path)
            await service.ensure(http,NOW)
            await DailyWeather(path).ensure(http,NOW+timedelta(hours=8))
            self.assertEqual(http.count,1)
            await service.ensure(http,NOW+timedelta(days=1))
            self.assertEqual(http.count,2)

    async def test_failure_does_not_silently_retry_same_day(self):
        with tempfile.TemporaryDirectory() as folder:
            service=DailyWeather(Path(folder)/'weather.db');http=WeatherHTTP(True)
            await service.ensure(http,NOW);await service.ensure(http,NOW)
            self.assertEqual(http.count,5)
            self.assertEqual(service.read('2026-10-28')['status'],'unavailable')

    async def test_second_provider_and_daily_success_cache(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'weather.db';service=DailyWeather(path)
            forecast=dict(date='2026-10-28',location='山西太原尖草坪',source='MET Norway',summary='备用预报')
            with patch.object(service,'fetch',AsyncMock(side_effect=[OSError('offline'),forecast])) as fetch:
                result=await service.ensure(object(),NOW)
                self.assertEqual(result['source'],'MET Norway')
                self.assertEqual(fetch.await_count,2)
                await DailyWeather(path).ensure(object(),NOW)
                self.assertEqual(fetch.await_count,2)

    async def test_concurrent_request_has_one_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'weather.db';http=WeatherHTTP()
            await asyncio.gather(DailyWeather(path).ensure(http,NOW),DailyWeather(path).ensure(http,NOW))
            self.assertEqual(http.count,1)


class ScheduleTests(unittest.TestCase):
    def test_week_anchor_advances_on_monday(self):
        self.assertEqual(schedule.week_number(NOW.date()),8)
        self.assertEqual(schedule.week_number(datetime(2026,11,1).date()),8)
        self.assertEqual(schedule.week_number(datetime(2026,11,2).date()),9)

    def test_week_eight_wednesday_and_alternating_monday(self):
        data=schedule.today(NOW)
        self.assertEqual(len(data['courses']),3)
        self.assertIn('大学英语A(3)',data['possible'])
        self.assertTrue(data['uncertain'])
        self.assertFalse(schedule.in_weeks(6,'1-3,5,7,8-15'))
        self.assertTrue(schedule.in_weeks(8,'1-3,5,7,8-15'))

    def test_tuesday_laboratory_changes(self):
        eight=schedule.today(datetime(2026,10,27,20,tzinfo=NOW.tzinfo))
        nine=schedule.today(datetime(2026,11,3,20,tzinfo=NOW.tzinfo))
        self.assertIn('大物实验室1',[c['room'] for c in eight['courses']])
        self.assertIn('大物实验室2',[c['room'] for c in nine['courses']])


class MemoryTests(unittest.TestCase):
    def test_location_changes_record_history_and_keep_users_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            life.record('a',[dict(kind='location',value='太原',evidence='我在太原')],'我在太原',NOW)
            life.record('a',[dict(kind='location',value='北京',evidence='我到了北京')],'我到了北京',NOW)
            self.assertEqual(life.facts('a')[0]['value'],'北京')
            self.assertEqual(life.journal()[0]['previous'],'太原')
            self.assertEqual(life.facts('b'),[])
            self.assertEqual(len((Path(folder)/'conversation_journal.jsonl').read_text(encoding='utf-8').splitlines()),2)

    def test_wishes_and_hallucinated_location_are_not_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            for message,value in [('我想去北京','北京'),('假如我在北京','北京'),('我在太原','上海')]:
                self.assertEqual(life.record('a',[dict(kind='location',value=value,evidence=message)],message,NOW),[])

    def test_emotion_accumulates_and_decays_without_public_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            for _ in range(3):
                self.assertEqual(life.mood('a',{'anger':2},NOW),'自然平稳，跟随用户话题。')
            self.assertIn('稍直接',life.mood('a',{'anger':2},NOW))
            self.assertEqual(life.mood('a',{},NOW+timedelta(days=2)),'自然平稳，跟随用户话题。')
            self.assertEqual(life.journal(),[])
            self.assertNotIn('anger',life.profile('a'))

    def test_identifying_fields_are_redacted(self):
        self.assertNotIn('13812345678',redact('电话 13812345678'))
        self.assertNotIn('abc@example.com',redact('邮箱 abc@example.com'))
        self.assertEqual(redact('学号：2501054136'),'')


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_extraction_cached_and_no_raw_content_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=ContextEngine(folder)
            async def notes(*args):return {'pages':[{'text':'只想得到温和直接的回应。邮箱 abc@example.com'}]}
            class HTTP:
                def __init__(self):self.count=0;self.request=None
                def post(self,url,**kwargs):
                    self.count+=1;self.request=kwargs['json']
                    return Response({'choices':[{'message':{'content':'{"traits":["gentle","direct","某个私人事件"]}'}}]})
            http=HTTP()
            with patch('context_engine.read_notes',notes),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
                first=await engine.personality(http,NOW)
                second=await engine.personality(http,NOW)
            self.assertEqual(first,second);self.assertEqual(http.count,1)
            self.assertEqual(len(first),2)
            cache=(Path(folder)/'data'/'note_profile.json').read_text(encoding='utf-8')
            self.assertNotIn('abc@example.com',str(http.request))
            self.assertNotIn('只想得到',cache)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.controller=Dashboard(self.temp.name,token='local-test')
        self.runner=web.AppRunner(self.controller.app());await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.http=ClientSession()
    async def asyncTearDown(self):
        await self.http.close();await self.runner.cleanup();self.temp.cleanup()
    async def test_api_requires_token_and_exposes_no_mood(self):
        async with self.http.get(self.url+'/api/status') as r:self.assertEqual(r.status,401)
        async with self.http.get(self.url+'/api/status',headers={'Authorization':'Bearer local-test'}) as r:
            data=await r.json();self.assertEqual(r.status,200)
            for name in ('affinity','anger','feelings','emotion','traits'):
                self.assertNotIn(name,data)
            self.assertNotIn(':',data['hour'].split()[-1])
    async def test_cross_origin_post_is_rejected(self):
        async with self.http.post(self.url+'/api/preferences',json={},headers={'Authorization':'Bearer local-test','Origin':'https://unrelated.invalid'}) as r:
            self.assertEqual(r.status,403)
    async def test_status_and_preference_ack_show_effective_global_mode(self):
        cfg={'spontaneous_images':True,'proactive_enabled':True}
        with patch.dict('os.environ',{'IMAGE_MODE':'off'}),patch('web_manager.preferences.load',return_value=cfg),patch('web_manager.preferences.save'):
            async with self.http.get(self.url+'/api/status',headers={'Authorization':'Bearer local-test'}) as response:
                data=await response.json();self.assertFalse(data['drawing_effective']['spontaneous_enabled']);self.assertFalse(data['drawing_effective']['proactive_enabled'])
            async with self.http.post(self.url+'/api/preferences',json={'proactive_enabled':True},headers={'Authorization':'Bearer local-test'}) as response:
                data=await response.json();self.assertTrue(data['ok']);self.assertEqual(data['drawing_effective']['reason'],'global_mode_off')


if __name__=='__main__':unittest.main()
