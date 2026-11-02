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


class MemoryTests(unittest.TestCase):
    def test_location_changes_record_history_and_keep_users_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            life.record('a',[dict(kind='location',value='城市甲',evidence='我在城市甲')],'我在城市甲',NOW)
            life.record('a',[dict(kind='location',value='北京',evidence='我到了北京')],'我到了北京',NOW)
            self.assertEqual(life.facts('a')[0]['value'],'北京')
            self.assertEqual(life.journal()[0]['previous'],'城市甲')
            self.assertEqual(life.facts('b'),[])
            self.assertEqual(len((Path(folder)/'conversation_journal.jsonl').read_text(encoding='utf-8').splitlines()),2)

    def test_wishes_and_hallucinated_location_are_not_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            for message,value in [('我想去北京','北京'),('假如我在北京','北京'),('我在城市甲','城市乙')]:
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


if __name__=='__main__':unittest.main()
