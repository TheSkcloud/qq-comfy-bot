import asyncio
import copy
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
import media_input
from life import Life
from reply_scheduler import ReplyScheduler, CURRENT, Superseded
from response_plan import parse, update_life, life_context


class PlanTests(unittest.TestCase):
    def test_silent_reaction_and_semantic_segments(self):
        self.assertEqual(parse({'response_plan':{'should_reply':False}})['segments'],[])
        self.assertEqual(parse({'reaction':'shy','response_plan':{'mode':'reaction'}})['mode'],'reaction')
        self.assertEqual(parse({'reply':'compatibility','response_plan':{'segments':['一句','第二句']}})['segments'],['一句','第二句'])
        with self.assertRaises(ValueError):
            parse({'reply':''})
        with self.assertRaises(ValueError):
            parse({'response_plan':{'segments':['a']*5}})

    def test_life_state_expiry_and_no_sliding_renewal(self):
        now=datetime(2026,10,31,14,tzinfo=ZoneInfo('Asia/Shanghai'))
        session={'state':copy.deepcopy(bot.DEFAULT)}
        data={'life_update':{'activity':'在房间看书','minutes':20}}
        update_life(session,data,now)
        expiry=session['life_state']['expires_at']
        update_life(session,data,now+timedelta(minutes=5))
        self.assertEqual(session['life_state']['expires_at'],expiry)
        self.assertIn('已保存',life_context(session,now+timedelta(minutes=5)))
        self.assertIn('过有效期',life_context(session,now+timedelta(minutes=25)))
        session['state']['character']='另一个原创角色'
        self.assertEqual(life_context(session,now),'')

    def test_explicit_correction_keeps_history_and_user_isolation(self):
        now=datetime.now(ZoneInfo('Asia/Shanghai'))
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder)
            for user in ('42','99'):
                life.record(user,[{'kind':'preference','value':'喜欢奶茶','evidence':'我喜欢奶茶'}],'我喜欢奶茶',now)
            self.assertTrue(life.correct('42','喜欢奶茶','现在更喜欢清茶',now))
            self.assertEqual(life.facts('42')[0]['value'],'现在更喜欢清茶')
            self.assertEqual(life.facts('99')[0]['value'],'喜欢奶茶')
            self.assertEqual(life.journal()[0]['previous'],'喜欢奶茶')


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_computation_is_cancelled_on_revision(self):
        scheduler=ReplyScheduler();ticket=scheduler.begin('k',10)
        started=asyncio.Event();cancelled=asyncio.Event()
        async def model():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task=asyncio.create_task(scheduler.run(ticket,model()))
        await started.wait();scheduler.begin('k',20)
        with self.assertRaises(Superseded):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_typing_wait_is_interruptible_and_sessions_are_isolated(self):
        scheduler=ReplyScheduler();ticket=scheduler.begin('k',10)
        task=asyncio.create_task(scheduler.wait(ticket,60))
        await asyncio.sleep(0)
        other=scheduler.begin('other',10)
        scheduler.check(ticket)
        scheduler.begin('k',20)
        with self.assertRaises(Superseded):
            await task
        scheduler.check(other)

    async def test_already_dispatched_packet_can_finish_commit(self):
        scheduler=ReplyScheduler();ticket=scheduler.begin('k',10)
        ack=asyncio.Event();started=asyncio.Event();committed=[]
        async def send():
            ticket.in_flight=True;started.set()
            await ack.wait()
            ticket.packets+=1;ticket.in_flight=False;committed.append('actual delivery')
            scheduler.check(ticket)
        task=asyncio.create_task(scheduler.run(ticket,send()))
        await started.wait();scheduler.begin('k',20);ack.set()
        with self.assertRaises(Superseded):
            await task
        self.assertEqual(committed,['actual delivery'])

    async def test_elapsed_model_time_is_not_added_twice(self):
        scheduler=ReplyScheduler();ticket=scheduler.begin('k',30)
        ticket.started=time.monotonic()-40
        self.assertGreater(scheduler.remaining(ticket,100),0)
        self.assertLessEqual(scheduler.remaining(ticket,100),scheduler.timing['typing_max']*(1+scheduler.timing['jitter']))
        self.assertGreater(scheduler.remaining(ticket,80,segment=1),0)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.root_patch=patch.object(bot,'ROOT',self.root);self.root_patch.start()
        self.instance=bot.Bot(None);self.instance.connected.set()
        # Transport tests exercise admission success; probability gates have their own tests.
        self.transport_preferences=bot.preferences.load()
        self.transport_preferences['drawing'].update(initiative=1,comfort=1,comic=1)
        self.preferences_patch=patch.object(bot.preferences,'load',side_effect=lambda:copy.deepcopy(self.transport_preferences))
        self.preferences_patch.start();self.addCleanup(self.preferences_patch.stop)
        self.instance.context.build=AsyncMock(return_value='')
        self.instance.indicate_typing=AsyncMock()
        self.instance.scheduler.remaining=lambda *args:0
        self.packets=[]
        async def call(action,params,ws):
            self.packets.append((action,params));return {'message_id':len(self.packets)}
        self.instance.call=call
        self.event={'post_type':'message','message_type':'private','user_id':42,'message_id':1,'message':[]}
        self.key='private:0:42'
        self.data={'reply':'最新回复','state':copy.deepcopy(bot.DEFAULT),'send_image':False,'visual_tags':'','reaction':'none'}

    async def asyncTearDown(self):
        for task in list(self.instance.tasks):
            task.cancel()
        await asyncio.gather(*list(self.instance.tasks),return_exceptions=True)
        self.instance.store.db.close();self.root_patch.stop();self.temp.cleanup()

    async def test_new_supplement_replaces_unsent_reply_and_merges_input(self):
        started=asyncio.Event();calls=[]
        async def model(http,state,history,text,*args,**kwargs):
            calls.append(text)
            if len(calls)==1:
                started.set();await asyncio.Event().wait()
            return copy.deepcopy(self.data)
        with patch.object(bot,'deepseek',model):
            self.instance.enqueue(self.event,self.key,'我今天去了图书馆')
            await asyncio.wait_for(started.wait(),2)
            self.instance.enqueue(self.event,self.key,'不对，刚才说错了，是咖啡馆')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        sent=[p[1]['message'][0]['data'].get('text') for p in self.packets]
        self.assertEqual(sent,['最新回复'])
        self.assertIn('图书馆\n不对',calls[-1])
        history=self.instance.store.get(self.key)['history']
        self.assertEqual(len([m for m in history if m['role']=='assistant']),1)

    async def test_interrupt_between_segments_blocks_text_and_sticker(self):
        waiting=asyncio.Event()
        def delay(ticket,length,segment=0):
            if segment:
                waiting.set();return 60
            return 0
        self.instance.scheduler.remaining=delay
        old=copy.deepcopy(self.data);old.update(reaction='happy',response_plan={'segments':['第一条','不应发送的第二条']})
        async def model(http,state,history,text,*args,**kwargs):
            return old if text=='旧话题' else copy.deepcopy(self.data)
        with patch.object(bot,'deepseek',model):
            self.instance.enqueue(self.event,self.key,'旧话题')
            await asyncio.wait_for(waiting.wait(),2)
            self.instance.enqueue(self.event,self.key,'现在换个话题')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        sent=[packet[1]['message'][0] for packet in self.packets]
        self.assertEqual([p['data'].get('text') for p in sent],['第一条','最新回复'])
        self.assertTrue(all(p['type']=='text' for p in sent))

    async def test_media_only_does_not_claim_vision_or_call_text_model(self):
        event={**self.event,'message':[{'type':'image','data':{'url':'https://example.invalid/a.png'}}]}
        with patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.handle(event,self.key,'[收到图片]')
            model.assert_not_called()
        text=self.packets[0][1]['message'][0]['data']['text']
        self.assertIn('没识别',text)
        self.assertEqual(self.instance.store.get(self.key)['last_media']['status'],'unavailable')

    async def test_silent_plan_records_user_but_sends_no_packets(self):
        self.data['response_plan']={'should_reply':False,'mode':'silent','segments':[]}
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.data)):
            await self.instance.handle(self.event,self.key,'嗯')
        self.assertEqual(self.packets,[])
        self.assertEqual([m['role'] for m in self.instance.store.get(self.key)['history']],['user'])

    async def test_ack_after_completed_statement_ends_without_a_model_bill(self):
        session=self.instance.store.get(self.key)
        session['history']=[{'role':'assistant','content':'那这几天没白忙。'}]
        self.instance.store.save(self.key,session)
        with patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.handle(self.event,self.key,'嗯')
            model.assert_not_called()
        self.assertEqual(self.packets,[])
        self.assertEqual(self.instance.store.get(self.key)['history'][-1]['role'],'user')

    async def test_reaction_only_has_no_leaked_internal_json(self):
        folder=self.root/'assets'/'reactions';folder.mkdir(parents=True);(folder/'happy.png').write_bytes(b'png')
        self.data.update(reaction='happy',response_plan={'mode':'reaction','segments':[]})
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.data)):
            await self.instance.handle(self.event,self.key,'哈哈')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(self.packets[0][1]['message'][0]['type'],'image')

    async def test_explicit_photo_is_not_lost_when_text_plan_is_silent(self):
        self.data.update(visual_tags='adult woman, reading',response_plan={'mode':'silent','should_reply':False})
        self.data['visual_scene']=dict(identity='woman',outfit='home clothing',setting='room',action='reading',composition='medium shot')
        from test_planning_fixture import creative
        self.data.update(creative_brief=creative(),_thinking_enabled=True)
        self.instance.comfy.image=AsyncMock(return_value=b'png')
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.data)),patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True,'test_stub':True})):
            await self.instance.handle(self.event,self.key,'照片')
            await asyncio.gather(*list(self.instance.tasks))
        self.assertEqual([p[1]['message'][0]['type'] for p in self.packets],['image'])

    async def test_accept_preserves_images_and_does_not_dedup_missing_ids(self):
        self.instance.users={42}
        event={**self.event,'message':[{'type':'image','data':{'url':'https://gchat.qpic.cn/a'}}]}
        self.assertIn('内容尚未识别',self.instance.accept(event)[1])
        self.assertIsNone(self.instance.accept(event))
        event.pop('message_id')
        self.assertIsNotNone(self.instance.accept(event))
        self.assertIsNotNone(self.instance.accept(event))

    async def test_cancel_command_does_not_regenerate_interrupted_input(self):
        started=asyncio.Event();calls=[]
        async def model(*args,**kwargs):
            calls.append(args[3]);started.set();await asyncio.Event().wait()
        with patch.object(bot,'deepseek',model):
            self.instance.enqueue(self.event,self.key,'旧问题')
            await asyncio.wait_for(started.wait(),2)
            self.instance.enqueue(self.event,self.key,'/取消')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        self.assertEqual(calls,['旧问题'])
        self.assertEqual([p[1]['message'][0]['data']['text'] for p in self.packets],['好，先停下。'])


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_visual_config_does_not_spend_requests(self):
        with patch.dict(os.environ,{'VISION_API_KEY':'','VISION_MODEL':'','VISION_BASE_URL':'','NATIVE_VISION_ENABLED':'0'}):
            http=type('NoNetwork',(),{'post':lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('Unexpected request'))})()
            value=await media_input.observe(http,[{'type':'image','data':{'url':'https://gchat.qpic.cn/a'}}])
        self.assertEqual(value['status'],'unavailable')
        self.assertIn('不能声称',media_input.evidence_context(value))

    async def test_inline_payload_is_never_persisted_as_media_history(self):
        observation={'status':'attached','images':1,'blocks':[{'type':'image_url','image_url':{'url':'data:private-image'}}]}
        result=media_input.finalize(observation,{'status':'recognized','description':'一张写有文字的表情'})
        self.assertEqual(result['status'],'recognized')
        self.assertNotIn('blocks',result)
        self.assertNotIn('private-image',str(result))
        self.assertEqual(media_input.finalize(observation,{})['status'],'unclear')


class ProviderResponse:
    status=200
    def __init__(self,content):self.content=content
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def json(self):return {'choices':[{'message':{'content':self.content},'finish_reason':'stop'}]}


class Provider:
    def __init__(self,content):self.content=content;self.requests=[]
    def post(self,*args,**kwargs):
        self.requests.append(kwargs['json'])
        return ProviderResponse(self.content)


class SinglePassTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_optional_fields_do_not_spend_a_retry(self):
        import json
        provider=Provider(json.dumps({'reply':'嗯。','state':None,'waiting_lines':None,'affect':None}))
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test','CHAT_SCHEMA_RETRY':'0','SPEECH_REVIEW_ENABLED':'0'}):
            data=await bot.deepseek(provider,bot.DEFAULT,[],'嗯')
        self.assertEqual(len(provider.requests),1)
        self.assertEqual(data['state'],bot.DEFAULT)
        self.assertEqual(provider.requests[0]['thinking'],{'type':'disabled'})

    async def test_native_images_are_in_the_single_user_request(self):
        import json
        provider=Provider(json.dumps({'reply':'图上有文字。','media_analysis':{'status':'recognized','description':'文字'}}))
        media={'status':'attached','images':1,'blocks':[{'type':'image_url','image_url':{'url':'data:image/png;base64,dGVzdA=='}}]}
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test','CHAT_SCHEMA_RETRY':'0','SPEECH_REVIEW_ENABLED':'0'}):
            await bot.deepseek(provider,bot.DEFAULT,[],'图上是什么',media=media)
        self.assertEqual(len(provider.requests),1)
        messages=provider.requests[0]['messages']
        self.assertEqual(messages[-1]['role'],'user')
        self.assertEqual(messages[-1]['content'][1]['type'],'image_url')

    async def test_invalid_json_default_never_retries(self):
        provider=Provider('{')
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test','CHAT_SCHEMA_RETRY':'0'}):
            with self.assertRaises(ValueError):
                await bot.deepseek(provider,bot.DEFAULT,[],'你好')
        self.assertEqual(len(provider.requests),1)


if __name__=='__main__':
    unittest.main()
