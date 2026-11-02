import asyncio,copy,json,tempfile,time,unittest,os
from pathlib import Path
from unittest.mock import AsyncMock,patch,Mock
import bot,dialogue_depth as depth,image_concept
from gpu_memory_manager import GPUManager
from test_planning_fixture import creative
import test_upgrade


class DepthTests(unittest.TestCase):
    def setUp(self):self.session={};self.current=bot.now()
    def turn(self,text='人物关系记下来以后，阅读时更容易接上前文。',data=None):
        decision=depth.plan(self.session,text,[],self.current)
        depth.finish(self.session,text,'我觉得这样能保留联系。',data or {},decision,self.current)
        return decision
    def test_four_completed_turns_enable_the_next_substantive_turn(self):
        for _ in range(4):self.assertFalse(self.turn()['thinking'])
        self.assertTrue(depth.plan(self.session,'不过前后人物变化还是需要留意。',[],self.current)['thinking'])
        self.assertEqual(self.session['dialogue_state']['substantive_turns'],4)
    def test_complex_first_turn_and_short_ack_in_deep_topic(self):
        self.assertTrue(depth.plan({},'请比较两个方案并认真分析代价',[],self.current)['thinking'])
        for _ in range(4):self.turn()
        for text in ('嗯','哈哈','好的','知道了'):
            self.assertFalse(self.turn(text)['thinking'])
        self.assertEqual(self.session['dialogue_state']['substantive_turns'],4)
    def test_fragments_count_once_and_cancelled_turn_does_not_count(self):
        d=depth.plan(self.session,'我觉得人物关系还是比较难记住。',[],self.current)
        self.assertNotIn('dialogue_state',self.session)
        depth.finish(self.session,'我觉得人物关系还是比较难记住。','第一条。\n第二条。',{},d,self.current)
        depth.finish(self.session,'我觉得人物关系还是比较难记住。','第一条。\n第二条。',{},d,self.current)
        self.assertEqual(self.session['dialogue_state']['substantive_turns'],1)
    def test_switch_side_and_resume_preserve_the_old_discussion(self):
        for _ in range(4):self.turn()
        side=self.turn('顺便问一下，午饭吃了什么？')
        self.assertEqual(side['relation'],'side');self.assertEqual(self.session['dialogue_state']['substantive_turns'],4)
        self.assertFalse(self.turn('换个话题，今天窗外的树长得挺茂盛。')['thinking'])
        self.assertTrue(depth.plan(self.session,'回到刚才，人物关系确实很关键。',[],self.current)['thinking'])
    def test_semantic_switch_and_grounded_summary_update_use_existing_output(self):
        for _ in range(4):self.turn()
        user='其实我现在谈的是音乐的节奏。'
        self.turn(user,{'dialogue_update':{'relation':'switch','evidence':'音乐的节奏','topic_summary':'音乐节奏',
            'important_context':[{'speaker':'user','text':'用户来自北京','evidence':'不存在的原话'}]}})
        s=self.session['dialogue_state'];self.assertEqual(s['substantive_turns'],1)
        self.assertEqual(s['topic_summary'],'音乐节奏');self.assertNotIn('北京',json.dumps(s,ensure_ascii=False))
    def test_discussion_context_survives_short_history(self):
        for i in range(8):self.turn('人物关系需要留意，我这次记录了第'+str(i)+'条发现。')
        self.assertIn('第0条',depth.context(depth.plan(self.session,'我想接着聊这个观察。',[],self.current)))
    def test_short_substantive_followup_to_complex_question_keeps_thinking(self):
        self.turn('请认真分析两个算法的内存和维护成本。')
        self.assertTrue(depth.plan(self.session,'那第二种呢？',[],self.current)['thinking'])
        self.assertFalse(depth.plan(self.session,'好的',[],self.current)['thinking'])


class ConceptTests(unittest.TestCase):
    def test_full_brief_is_distinct_from_camera_and_applies_to_visual_action(self):
        value=creative('mid dance step');self.assertEqual(set(image_concept.validate(value)),set(image_concept.FIELDS))
        data={'creative_brief':value,'image_plan':{'camera':'side view'}}
        self.assertTrue(image_concept.apply(data));self.assertEqual(data['image_plan']['camera'],'side view')
        self.assertIn('mid dance step',data['image_plan']['action'])
    def test_missing_malformed_or_non_english_visual_fields_need_repair(self):
        for value in (None,[],{'concept':'x'},{**creative(),'action':'跳舞'}):
            self.assertTrue(image_concept.needs_plan({'creative_brief':value}))
        self.assertTrue(image_concept.needs_plan({'creative_brief':creative(),'_thinking_enabled':False}))
    def test_director_camera_responds_to_creative_focal_point(self):
        import shot_director
        base={'style':'life','action':'reading a book','props':'book','camera':'medium shot','setting_tags':'room desk'}
        hands=shot_director.choose({**base,'creative_brief':{**creative(),'focal_point':'fingertips at the page corner'}})
        face=shot_director.choose({**base,'creative_brief':{**creative(),'focal_point':'her face and thoughtful gaze'}})
        self.assertEqual(hands['size'],'detail');self.assertEqual(face['size'],'close')


def telemetry(reserved=3*1024**3):
    return {'devices':[dict(index=0,type='cuda',vram_total=12*1024**3,vram_free=4*1024**3,torch_vram_total=reserved,torch_vram_free=0)]}


class GPUTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.clock=[10000.]
        self.m=GPUManager(None,'http://127.0.0.1:8188',self.root,clock=lambda:self.clock[0],config={'enabled':True,'timeout':3600,'interval':60})
        self.queue={'queue_running':[],'queue_pending':[]};self.freed=False;self.posts=[]
        async def get(route):
            if route=='/queue':return copy.deepcopy(self.queue)
            if route.startswith('/history'):return {}
            return telemetry(0 if self.freed else 3*1024**3)
        self.m.get=get
        parent=self
        class Response:
            status=200
            def raise_for_status(self):pass
            async def read(self):return b''
            async def __aenter__(self):parent.freed=True;return self
            async def __aexit__(self,*args):pass
        class HTTP:
            def post(self,url,**kwargs):parent.posts.append((url,kwargs));return Response()
        self.m.http=HTTP()
    async def asyncTearDown(self):self.temp.cleanup()
    async def test_idle_deadline_and_exact_free_parameters(self):
        self.clock[0]+=3599;self.assertEqual((await self.m.tick())['reason'],'not_idle_long_enough')
        self.clock[0]+=1;result=await self.m.tick();self.assertEqual(result['reason'],'unloaded')
        self.assertEqual(self.posts[0][1]['json'],{'unload_models':True,'free_memory':True})
        self.assertEqual(result['torch_reserved_released_bytes'],3*1024**3)
    async def test_external_running_pending_and_internal_tasks_never_unload(self):
        self.clock[0]+=4000
        for field in ('queue_running','queue_pending'):
            self.queue[field]=[['external-job']];self.assertEqual((await self.m.tick())['reason'],'external_queue_busy');self.queue[field]=[]
        self.m.internal_busy=lambda:True;self.assertEqual((await self.m.tick())['reason'],'internal_tasks');self.assertEqual(self.posts,[])
    async def test_unknown_submission_and_unknown_state_never_unload(self):
        self.clock[0]+=4000;self.m.submitting('unknown')
        self.assertEqual((await self.m.tick())['reason'],'submission_outcome_unknown')
        self.m.get=AsyncMock(side_effect=ValueError('bad queue'))
        self.assertEqual((await self.m.tick())['reason'],'state_or_interface_unavailable');self.assertFalse(self.posts)
    async def test_failed_interface_is_nonfatal(self):
        self.clock[0]+=4000
        self.m.get=AsyncMock(side_effect=asyncio.TimeoutError())
        self.assertEqual((await self.m.tick())['reason'],'state_or_interface_unavailable')
    async def test_new_arrival_waits_for_unload_then_executes(self):
        entered=asyncio.Event();release=asyncio.Event();order=[]
        async def settle():entered.set();await release.wait();self.m.unload_pending=False;order.append('unloaded');return True
        self.m.unload_pending=True;self.m.settle=settle
        async def execute():
            async with self.m.execution():order.append('submit')
        task=asyncio.create_task(execute());await entered.wait();self.assertEqual(self.m.active,1)
        release.set();await task;self.assertEqual(order,['unloaded','submit'])
    async def test_new_task_detected_by_final_queue_recheck(self):
        self.clock[0]+=4000;original=self.m.get;checks=0
        async def get(route):
            nonlocal checks
            if route=='/queue':
                checks+=1
                if checks==2:return {'queue_running':[],'queue_pending':[['new-external']]}
            return await original(route)
        self.m.get=get;self.assertEqual((await self.m.tick())['reason'],'queue_recheck_busy');self.assertFalse(self.posts)
    async def test_completion_clock_and_restart_are_conservative(self):
        self.m.submitting('t');self.clock[0]+=100;self.m.submitted('t','p')
        self.assertEqual(self.m.state['last_activity_at'],10000)
        self.clock[0]+=100;self.m.completed('t');self.assertEqual(self.m.state['last_activity_at'],10200)
        restarted=GPUManager(None,self.m.url,self.root,clock=lambda:self.clock[0],config=self.m.config)
        self.assertEqual(restarted.state['last_activity_at'],10200)
    async def test_constructing_a_diagnostic_client_does_not_mutate_live_ledger(self):
        self.m.submitting('live-unknown');original=self.m.path.read_bytes()
        GPUManager(None,self.m.url,self.root,clock=lambda:self.clock[0]+5000,config=self.m.config)
        self.assertEqual(self.m.path.read_bytes(),original)
    async def test_external_completed_history_resets_idle_time(self):
        self.clock[0]+=4000
        history={'p':{'status':{'completed':True,'messages':[['execution_success',{'timestamp':13900000}]]}}}
        original=self.m.get
        async def get(route):return history if route.startswith('/history') else await original(route)
        self.m.get=get;self.assertEqual((await self.m.tick())['reason'],'not_idle_long_enough')
        self.assertEqual(self.m.state['last_activity_at'],13900)
    async def test_no_memory_change_does_not_permanently_block_next_work(self):
        self.clock[0]+=4000;original=self.m.get
        async def get(route):return telemetry(0) if route=='/system_stats' else await original(route)
        self.m.get=get
        result=await self.m.tick();self.assertEqual(result['reason'],'unload_requested_no_measurable_change')
        self.assertFalse(self.m.unload_pending);self.assertFalse(self.m.state['unload_confirmed'])
        async with self.m.execution():pass


class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data
    async def finish(self):
        while any(not t.done() for t in self.instance.tasks):await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
    async def test_complete_thinking_concept_reuses_the_main_request(self):
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())),patch('image_planner.complete_visual',AsyncMock()) as extra,patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'画一张看书');await self.finish()
        extra.assert_not_called()
        job=json.loads(next((self.root/'data/image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertIn('creative_brief',job);self.assertIn('shot_plan',job['snapshot'])
    async def test_missing_concept_and_visual_data_repaired_once_without_rewriting_chat(self):
        broken=self.image_data();broken.pop('creative_brief');broken['visual_scene']={}
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=broken)) as main,patch('image_planner.complete_visual',AsyncMock(return_value=self.image_data())) as repair,patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'想看看你跳舞的样子');await self.finish()
        self.assertEqual(main.await_count,1);self.assertEqual(repair.await_count,1)
    async def test_failed_concept_preserved_as_a_traceable_failure_without_gpu(self):
        broken=self.image_data();broken['creative_brief']={'concept':'incomplete'}
        self.instance.comfy.image=AsyncMock()
        with patch.object(bot,'deepseek',AsyncMock(return_value=broken)),patch('image_planner.complete_visual',AsyncMock(return_value=broken)):
            await self.instance.handle(self.event,self.key,'画一张');await self.finish()
        self.instance.comfy.image.assert_not_called()
        job=json.loads(next((self.root/'data/image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['reason'],'creative_fields_missing');self.assertEqual(job['creative_brief'],{'concept':'incomplete'})


class FormatRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_thinking_final_body_gets_one_safe_text_only_repair(self):
        contexts=[]
        for content in ('','{"reply":"第二个方案更省内存。","state":{"clothing":"wrong"},"send_image":true}'):
            response=AsyncMock();response.status=200;response.raise_for_status=Mock()
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':content}}]}
            manager=AsyncMock();manager.__aenter__.return_value=response;contexts.append(manager)
        http=Mock();http.post.side_effect=contexts
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-test'}):
            value=await bot.deepseek(http,bot.DEFAULT,[],'请认真分析两个方案',False)
        self.assertEqual(http.post.call_count,2);self.assertEqual(value['state'],bot.DEFAULT)
        self.assertFalse(value['send_image']);self.assertTrue(value['_text_only_recovery'])
        self.assertEqual(http.post.call_args.kwargs['json']['thinking'],{'type':'disabled'})

    async def test_bad_or_empty_image_json_preserves_only_safe_reply_and_current_state(self):
        for content in ('{"reply":"原回复。",,"state":{"clothing":"wrong"}}',''):
            response=AsyncMock();response.status=200
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':content}}]}
            manager=AsyncMock();manager.__aenter__.return_value=response
            http=Mock();http.post.return_value=manager
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-test'}):
                value=await bot.deepseek(http,bot.DEFAULT,[],'画一张舞姿',True)
            self.assertEqual(value['state'],bot.DEFAULT);self.assertEqual(value['_model_format'],'image_plan_format_recovered')
            self.assertTrue(image_concept.needs_plan(value));self.assertEqual(http.post.call_count,1)

if __name__=='__main__':unittest.main()
