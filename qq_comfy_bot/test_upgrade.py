import asyncio, copy, io, json, os, unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo
from PIL import Image
import bot, dialogue, speech, initiative, image_director as d, image_workflow
import test_remediation

class EntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_remediation.PipelineTests.asyncSetUp(self)
        # Image entry tests must also run during the production quiet hours.
        self.clock_patch=patch.object(bot,'now',return_value=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        self.mode_patch=patch.dict(os.environ,{'IMAGE_MODE':'off'})
        self.mode_patch.start();self.addCleanup(self.mode_patch.stop)
        self.instance.context.routine.current_event=Mock(return_value={'activity':'在房间读书','event_id':'2026-11-01:14:reading'})
    asyncTearDown=test_remediation.PipelineTests.asyncTearDown

    def image_data(self):
        data=copy.deepcopy(self.data)
        data.update(send_image=True,visual_tags='woman, reading',visual_scene=dict(identity='woman',outfit='home clothing',setting='room',action='reading',composition='medium shot'))
        from test_planning_fixture import creative
        data.update(creative_brief=creative(),_thinking_enabled=True)
        return data

    async def workers(self):
        await asyncio.wait_for(asyncio.gather(*list(self.instance.inbox_workers.values())),3)

    async def test_three_messages_merge_at_real_entry(self):
        model=AsyncMock(return_value=self.data)
        with patch.object(bot,'deepseek',model):
            for text in ('第一件事','第二件事','第三件事'):
                self.instance.enqueue(self.event,self.key,text)
            await self.workers()
        self.assertEqual(model.await_count,1)
        self.assertEqual(model.call_args.args[3],'第一件事\n第二件事\n第三件事')

    async def test_chat_while_drawing_keeps_job_and_records_only_confirmed_image(self):
        drawing=asyncio.Event();release=asyncio.Event()
        async def draw(*a,**k):drawing.set();await release.wait();return b'image'
        self.instance.comfy.image=draw
        async def model(*a,**k):return self.image_data() if a[3]=='画一张' else copy.deepcopy(self.data)
        with patch.object(bot,'deepseek',model),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(drawing.wait(),3)
            job=self.instance.image_tasks[self.key]
            for text in ('我今天看了本书','内容挺有意思','还有个别的事'):
                self.instance.enqueue(self.event,self.key,text)
            await self.workers()
            self.assertFalse(job.done())
            self.assertFalse(any('图片已实际' in m['content'] for m in self.instance.store.get(self.key)['history']))
            release.set();await asyncio.wait_for(job,2)
        live=self.instance.store.get(self.key)
        self.assertEqual(sum('图片已实际' in m['content'] for m in live['history']),1)
        self.assertEqual(len(live['drawing_sends']),1)
        self.assertTrue(any('我今天看了本书' in m['content'] for m in live['history']))

    async def test_explicit_cancel_at_entry_prevents_late_image(self):
        drawing=asyncio.Event()
        async def draw(*a,**k):drawing.set();await asyncio.Event().wait()
        self.instance.comfy.image=draw
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(drawing.wait(),3)
            image_task=self.instance.image_tasks[self.key]
            self.instance.enqueue(self.event,self.key,'取消画图')
            await self.workers();await asyncio.wait_for(image_task,1)
        self.assertFalse(any(p[1]['message'][0]['type']=='image' for p in self.packets))
        self.assertFalse(self.instance.store.get(self.key).get('drawing_sends'))

    async def test_waiting_line_connected_and_stops_at_completion(self):
        drawing=asyncio.Event();release=asyncio.Event();line_sent=asyncio.Event()
        async def draw(*a,**k):drawing.set();await release.wait();return b'image'
        self.instance.comfy.image=draw
        data=self.image_data();data['waiting_lines']=['刚才那件事还挺有意思。','不应在完成后发送的句子']
        original=self.instance.call
        async def call(action,params,ws):
            result=await original(action,params,ws)
            if params.get('message',[{}])[0].get('data',{}).get('text')==data['waiting_lines'][0]:line_sent.set()
            return result
        self.instance.call=call
        cfg={**bot.preferences.load(),'image_chat':True,'image_chat_interval':5,'image_chat_max':1}
        with patch.object(bot.preferences,'load',return_value=cfg),patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(drawing.wait(),3)
            await asyncio.wait_for(line_sent.wait(),7)
            release.set();await asyncio.wait_for(self.instance.image_tasks[self.key],2)
        texts=[p[1]['message'][0].get('data',{}).get('text') for p in self.packets]
        self.assertEqual(texts.count(data['waiting_lines'][0]),1)
        self.assertNotIn(data['waiting_lines'][1],texts)

    async def test_busy_prevents_autonomous_candidate(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s=self.instance.store.get(self.key)
        s.update(last_user_at=(current-timedelta(hours=1)).isoformat(),busy_until=(current+timedelta(hours=1)).isoformat())
        cfg={**bot.preferences.load(),'proactive_enabled':True,'drawing':d.config({'initiative':1})}
        self.instance.store.save(self.key,s);self.instance.users={42}
        with patch.object(bot,'now',return_value=current),patch.object(bot.preferences,'load',return_value=cfg),patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.initiative_tick();model.assert_not_called()

    async def test_proactive_without_inbound_message_and_no_fake_user_history(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s=self.instance.store.get(self.key);s['last_user_at']=(current-timedelta(hours=1)).isoformat();self.instance.store.save(self.key,s);self.instance.users={42}
        cfg={**bot.preferences.load(),'proactive_enabled':True,'drawing':d.config({'initiative':1})}
        data=self.image_data();data['life_update']={'activity':'在房间读书','minutes':30};data['image_plan']={'kind':'life','evidence':'读书','relevance':1}
        self.instance.comfy.image=AsyncMock(return_value=b'image')
        with patch.object(bot,'now',return_value=current),patch.object(bot.preferences,'load',return_value=cfg),patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',AsyncMock(return_value=data)) as model,patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.initiative_tick()
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),2)
            await self.instance.initiative_tick()
        self.assertEqual(model.await_count,1)
        history=self.instance.store.get(self.key)['history']
        self.assertEqual([m['role'] for m in history],['assistant'])
        self.assertEqual([m['type'] for m in self.packets[-1][1]['message']],['text','image'])
        self.assertIn('这一会儿',self.packets[-1][1]['message'][0]['data']['text'])

    async def test_quality_budget_notifies_even_after_new_chat_revision(self):
        self.instance.comfy.image=AsyncMock(return_value=b'image')
        checking=asyncio.Event();release=asyncio.Event()
        async def inspect(*a):checking.set();await release.wait();return {'pass':False,'status':'qa_daily_budget_exhausted'}
        async def model(*a,**k):return self.image_data() if a[3]=='画一张' else copy.deepcopy(self.data)
        with patch.object(bot,'deepseek',model),patch.object(d,'inspect_image',inspect):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(checking.wait(),3)
            task=self.instance.image_tasks[self.key]
            self.instance.enqueue(self.event,self.key,'我先说件别的事')
            await self.workers();release.set();await asyncio.wait_for(task,2)
        self.assertTrue(any('检查额度' in p[1]['message'][0]['data'].get('text','') for p in self.packets))
        self.assertFalse(self.instance.store.get(self.key).get('drawing_sends'))

    async def test_disabling_initiative_during_gpu_work_prevents_send(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s=self.instance.store.get(self.key);s['last_user_at']=(current-timedelta(hours=1)).isoformat();self.instance.store.save(self.key,s);self.instance.users={42}
        cfg={**bot.preferences.load(),'proactive_enabled':True,'drawing':d.config({'initiative':1})}
        data=self.image_data();data['life_update']={'activity':'在房间读书','minutes':30};data['image_plan']={'kind':'life','evidence':'读书','relevance':1}
        started=asyncio.Event();release=asyncio.Event()
        async def draw(*a,**k):started.set();await release.wait();return b'image'
        self.instance.comfy.image=draw
        with patch.object(bot,'now',return_value=current),patch.object(bot.preferences,'load',side_effect=lambda:cfg.copy()),patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.initiative_tick()
            await asyncio.wait_for(started.wait(),2)
            cfg['proactive_enabled']=False;release.set()
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),2)
        self.assertEqual(self.packets,[])

    async def test_candidate_model_does_not_hold_chat_lock(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s=self.instance.store.get(self.key);s['last_user_at']=(current-timedelta(hours=1)).isoformat();self.instance.store.save(self.key,s);self.instance.users={42}
        cfg={**bot.preferences.load(),'proactive_enabled':True,'drawing':d.config({'initiative':1})}
        started=asyncio.Event();release=asyncio.Event()
        async def model(*a,**k):started.set();await release.wait();return copy.deepcopy(self.data)
        with patch.object(bot,'now',return_value=current),patch.object(bot.preferences,'load',return_value=cfg),patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',model):
            candidate=asyncio.create_task(self.instance.initiative_tick())
            await asyncio.wait_for(started.wait(),2)
            async with asyncio.timeout(1):
                async with self.instance.locks[self.key]:pass
            release.set();await candidate

class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_local_social_clues_generalize(self):
        for message in ('我这是在学你说话啊','你刚刚那句口气好怪','你话里有话吧'):
            self.assertEqual(speech.plan(message,[])['mode'],'teasing')
        self.assertTrue(dialogue.interaction('自画像怎么又画自己',[{'content':'在画头像'}])['recursive'])
        self.assertEqual(speech.plan('不好笑，别解释了',[])['mode'],'exit')
        self.assertTrue(speech.issues('看出来一点。','你知道我话里有话吗',[]))

    def test_typing_is_preserved_after_slow_generation_and_fast_cases_are_short(self):
        scheduler=bot.ReplyScheduler();t=scheduler.begin('k',500);t.started-=90
        normal=scheduler.remaining(t,150);self.assertGreater(normal,5)
        t.pace='fast';self.assertLess(scheduler.remaining(t,150),1.5)
        t.pace='normal';self.assertGreater(scheduler.remaining(t,150,1),scheduler.remaining(t,5,1))

    def test_initiative_switch_quiet_busy_daily_and_absence(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s={'last_user_at':(current-timedelta(hours=1)).isoformat()}
        cfg={'proactive_enabled':True,'drawing':{'life_candidates_per_day':2}}
        self.assertTrue(initiative.eligible(s,current,cfg))
        self.assertFalse(initiative.eligible(s,current,{**cfg,'proactive_enabled':False}))
        self.assertFalse(initiative.eligible(s,current,cfg,False))
        self.assertFalse(initiative.eligible(s,current.replace(hour=23),cfg))
        s['initiative_attempts']=[{'day':'2026-11-01'}]*2
        self.assertFalse(initiative.eligible(s,current,cfg))

    async def test_avatar_changes_framing_and_output_size(self):
        current=datetime.now(ZoneInfo('Asia/Shanghai'))
        s={'state':copy.deepcopy(bot.DEFAULT)}
        shot=d.freeze(s,{'_image_request':'给自己画个QQ头像'}, {'style':'life'},current,{})
        self.assertIn('head and shoulders',shot['camera'])
        graph=json.loads((Path(__file__).parent/'workflow_h_api.json').read_text(encoding='utf-8'))
        graph=await image_workflow.apply(None,'',graph,{'square_avatar':True})
        self.assertEqual(graph['68']['inputs']['width'],graph['68']['inputs']['height'])

    async def test_qa_budget_default_withholds_and_degraded_policy_is_explicit(self):
        import tempfile
        from pathlib import Path
        image=Image.effect_noise((256,256),50).convert('RGB');buf=io.BytesIO();image.save(buf,format='PNG')
        with tempfile.TemporaryDirectory() as folder:
            result=await d.inspect_image(None,buf.getvalue(),{},folder,{'qa_daily_budget':0})
            self.assertFalse(result['pass']);self.assertEqual(result['status'],'qa_daily_budget_exhausted')
            result=await d.inspect_image(None,buf.getvalue(),{'request_kind':'explicit'},folder,{'qa_daily_budget':0,'qa_exhausted_policy':'pixels_only'})
            self.assertTrue(result['pass']);self.assertTrue(result['degraded'])

if __name__=='__main__':unittest.main()
