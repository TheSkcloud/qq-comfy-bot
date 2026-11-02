import asyncio,copy,json,os,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from zoneinfo import ZoneInfo
import bot,image_candidates as c,image_director as d
import test_upgrade

class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    workers=test_upgrade.EntryTests.workers
    image_data=test_upgrade.EntryTests.image_data

    async def test_explicit_new_requests_queue_without_cancelling_active_image(self):
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def draw(prompt,job=None):
            calls.append(job.id)
            if len(calls)==1:started.set();await release.wait()
            job.record('submitted',prompt_id=job.id)
            job.record('generated',reason='fake_comfy_finished')
            return str(len(calls)).encode()
        self.instance.comfy.image=draw
        with patch.object(bot,'deepseek',AsyncMock(side_effect=lambda *a,**k:self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张书桌')
            await asyncio.wait_for(started.wait(),3)
            active=self.instance.image_active[self.key]
            self.instance.enqueue(self.event,self.key,'再拍一张')
            await self.workers()
            self.assertFalse(active.done());self.assertEqual(len(self.instance.image_queues[self.key]),1)
            release.set();await asyncio.wait_for(self.instance.image_tasks[self.key],3)
        live=self.instance.store.get(self.key)
        self.assertEqual(len(live['drawing_sends']),2)
        self.assertEqual(len(set(calls)),2)
        metrics=[json.loads(s) for s in (self.root/'data'/'delivery_metrics.jsonl').read_text().splitlines()]
        sent=[m for m in metrics if m['event']=='image_sent']
        self.assertEqual({m['image_task_id'] for m in sent},set(calls))
        decisions=[json.loads(s) for s in (self.root/'data'/'image_decisions.jsonl').read_text().splitlines()]
        self.assertTrue(set(calls)<={m.get('image_task_id') for m in decisions})

    async def test_comic_candidate_independent_of_chat_model_send_flag(self):
        spoken=copy.deepcopy(self.data);spoken['send_image']=False
        spoken['image_value']={'suitable':True,'kind':'comic','reason':'用漫画接住用户的模仿互动'}
        plan=self.image_data();plan['image_caption']='这回轮到我了。'
        model=AsyncMock(side_effect=[spoken,plan]);self.instance.comfy.image=AsyncMock(return_value=b'comic')
        cfg={**bot.preferences.load(),'drawing':d.config({'initiative':0,'comic':0})}
        with patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot.preferences,'load',return_value=cfg),patch.object(bot,'deepseek',model),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'我刚刚是在模仿你说话啦')
            await self.workers()
            for _ in range(20):
                if not self.instance.tasks:break
                await asyncio.gather(*list(self.instance.tasks))
        self.assertEqual(model.await_count,2)
        self.assertEqual(self.instance.store.get(self.key)['drawing_sends'][0]['kind'],'comic')
        last=self.packets[-1][1]['message']
        self.assertEqual([m['type'] for m in last],['text','image'])
        self.assertIn('这回轮到我了',self.instance.store.get(self.key)['history'][-1]['content'])

    async def test_comfort_from_context_does_not_mutate_fictional_life(self):
        spoken=copy.deepcopy(self.data);spoken.update(response_plan={'intent':'emotion','segments':['确实不顺。']},affect={'sadness':1},image_value={'suitable':True,'kind':'comfort','reason':'针对考试挫折的小画'})
        plan=self.image_data();plan['life_update']={'activity':'去用户家里','minutes':30}
        self.instance.comfy.image=AsyncMock(return_value=b'comfort')
        with patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',AsyncMock(side_effect=[spoken,plan])),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'今天的考试考砸了，心里挺堵的')
            await self.workers()
            for _ in range(20):
                if not self.instance.tasks:break
                await asyncio.gather(*list(self.instance.tasks))
        live=self.instance.store.get(self.key)
        self.assertEqual(live['drawing_sends'][0]['kind'],'comfort')
        self.assertNotIn('life_state',live)

    async def test_no_life_event_costs_no_request(self):
        current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        s=self.instance.store.get(self.key);s['last_user_at']=(current-timedelta(hours=1)).isoformat();self.instance.store.save(self.key,s)
        self.instance.users={42};self.instance.context.routine.current_event=Mock(return_value={'activity':'准备休息','event_id':'sleep'})
        cfg={**bot.preferences.load(),'proactive_enabled':True}
        with patch.object(bot,'now',return_value=current),patch.object(bot.preferences,'load',return_value=cfg),patch.dict(os.environ,{'IMAGE_MODE':'auto'}),patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.initiative_tick();model.assert_not_called()

    async def test_failed_generation_retry_is_bounded_and_send_once(self):
        import aiohttp
        self.instance.comfy.image=AsyncMock(side_effect=[aiohttp.ClientConnectionError(),b'success'])
        with patch.object(bot,'deepseek',AsyncMock(side_effect=lambda *a,**k:self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await self.workers();await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        self.assertEqual(self.instance.comfy.image.await_count,2)
        self.assertEqual(len(self.instance.store.get(self.key)['drawing_sends']),1)

    async def test_amendment_cancels_render_but_worker_processes_replacement(self):
        started=asyncio.Event();count=0
        async def draw(*a,**k):
            nonlocal count
            count+=1
            if count==1:started.set();await asyncio.Event().wait()
            return b'replacement'
        self.instance.comfy.image=draw
        with patch.object(bot,'deepseek',AsyncMock(side_effect=lambda *a,**k:self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(started.wait(),3)
            self.instance.enqueue(self.event,self.key,'改成侧脸')
            await self.workers()
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        self.assertEqual(count,2)
        self.assertEqual(len(self.instance.store.get(self.key)['drawing_sends']),1)

    async def test_same_conversation_pending_queue_respects_priority(self):
        started=asyncio.Event();release=asyncio.Event();order=[]
        async def draw(prompt,job=None):
            order.append(job.data['decision']['kind'])
            if len(order)==1:started.set();await release.wait()
            return str(len(order)).encode()
        self.instance.comfy.image=draw
        cfg={**bot.preferences.load(),'drawing':d.config({'daily_budget':4})}
        with patch.object(bot.preferences,'load',return_value=cfg),patch.object(bot,'deepseek',AsyncMock(side_effect=lambda *a,**k:self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(started.wait(),3)
            session=self.instance.store.get(self.key)
            # A life candidate joins the same production queue while first render is active.
            self.instance.launch_image(self.event,self.key,'',session,self.image_data(),{'kind':'life','style':'life','priority':30,'allowed':True,'reason':'test_candidate'},cfg)
            self.instance.enqueue(self.event,self.key,'再拍一张')
            await self.workers();release.set()
            await asyncio.wait_for(self.instance.image_tasks[self.key],3)
        self.assertEqual(order,['explicit','explicit','life'])

    async def test_queue_capacity_rejects_instead_of_unbounded_growth(self):
        cfg={**bot.preferences.load(),'drawing':d.config({'queue_limit':1})}
        future=asyncio.Event()
        self.instance.comfy.image=AsyncMock(side_effect=lambda *a,**k:future.wait())
        session=self.instance.store.get(self.key)
        decision={'kind':'explicit','style':'life','priority':0,'allowed':True}
        first=self.instance.launch_image(self.event,self.key,'画一张',session,self.image_data(),decision,cfg,True)
        second=self.instance.launch_image(self.event,self.key,'再拍一张',session,self.image_data(),decision,cfg,True)
        self.assertIsNotNone(first);self.assertIsNone(second)
        self.assertLessEqual(sum(len(q) for q in self.instance.image_queues.values()),1)

    async def test_input_end_never_guesses_an_undocumented_qq_event_code(self):
        self.instance.ws=object();self.instance.call=AsyncMock()
        await bot.Bot.indicate_typing(self.instance,self.event,True)
        await bot.Bot.indicate_typing(self.instance,self.event,False)
        self.assertEqual(self.instance.call.await_count,1)
        self.assertEqual(self.instance.call.call_args.args[1]['event_type'],1)
        self.assertEqual(self.instance.typing_users,set())
        self.instance.typing_users.add('42');self.instance.ws=None
        await bot.Bot.indicate_typing(self.instance,self.event,False)
        self.assertEqual(self.instance.typing_users,set())

    async def test_typing_cleared_after_entry_cancellation(self):
        waiting=asyncio.Event()
        def delay(*a):waiting.set();return 60
        self.instance.scheduler.remaining=delay
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.data)):
            self.instance.enqueue(self.event,self.key,'原问题')
            await asyncio.wait_for(waiting.wait(),3)
            self.instance.enqueue(self.event,self.key,'别回复了')
            await self.workers()
        self.assertEqual(self.packets,[])
        self.assertTrue(any(call.args[-1] is False for call in self.instance.indicate_typing.call_args_list if len(call.args)>1))

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.current=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.session={'state':copy.deepcopy(bot.DEFAULT),'history':[]}

    def test_structured_routine_candidate_is_stable_and_not_a_model_proposal(self):
        event={'activity':'在桌边画画','event_id':'today:14:draw'}
        first=c.life('private:0:42',self.session,self.current,event)
        second=c.life('private:0:42',self.session,self.current,event)
        self.assertEqual(first.dedupe_key,second.dedupe_key)
        self.assertEqual(first.location,self.session['state']['location'])
        data={'_candidate':first.data()}
        decision=d.decide(self.session,'',data,self.current,{'initiative':0})
        self.assertTrue(decision['allowed'])
        self.session['candidate_attempts']=[first.dedupe_key]
        self.assertEqual(d.decide(self.session,'',data,self.current,{})['reason'],'event_already_attempted')

    def test_global_budget_still_caps_independent_type_budgets(self):
        candidate=c.make('comic','k','event','reason','用户的玩笑',self.session,self.current)
        self.session['drawing_sends']=[{'day':'2026-11-01','kind':'life'}]*4
        decision=d.decide(self.session,'',{'_candidate':candidate.data()},self.current,{'daily_budget':4,'kind_limits':{'comic':{'daily_budget':2}}})
        self.assertFalse(decision['allowed']);self.assertEqual(decision['reason'],'daily_total_budget')

    def test_neutral_statement_and_failed_joke_do_not_force_candidate(self):
        self.assertIsNone(c.conversation('k',self.session,'今天整理了一下桌面',{'response_plan':{'intent':'share'},'affect':{}},self.current))
        self.assertIsNone(c.conversation('k',self.session,'不好笑，别解释了',{'response_plan':{'intent':'closing'}},self.current))

if __name__=='__main__':unittest.main()
