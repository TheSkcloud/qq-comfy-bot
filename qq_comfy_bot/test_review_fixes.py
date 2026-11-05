import asyncio,copy,hashlib,io,json,tempfile,time,unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
import bot,image_requests as requests,image_progress as progress,image_concept,dialogue_depth as depth,image_recovery
from visual_brief import assemble,complete_from_cache
from life import Life
import response_plan,test_upgrade

SELFIE='在房间的落地窗前发一张自拍，一只手比耶，一只手叉腰，穿着淡蓝色长裙，脚上是拖鞋和踩脚袜'

class PolicyTests(unittest.TestCase):
    def test_original_requests_and_contextual_continuations(self):
        session={'history':[]};current=bot.now()
        for text in ('对了，羽那能给我发一张自拍吗？',SELFIE):
            self.assertEqual(requests.classify(text,session,current)['action'],'request')
        for text in ('去画吧','再画画'):
            self.assertEqual(requests.classify(text,session,current)['action'],'unbound_confirm')
        requests.receive(session,requests.classify(SELFIE,session,current),current,'a'*32)
        for text in ('去画吧','再画画'):
            result=requests.classify(text,session,current)
            self.assertIn(SELFIE,result['text']);self.assertIn(result['action'],('confirm','retry'))
        self.assertEqual(requests.classify('去画吧',session,current+timedelta(days=2))['action'],'unbound_confirm')

    def test_denials_statements_and_code_do_not_request(self):
        for text in ('我在画画','不要自拍','看看代码','照片很好看','不用发图','画函数图像'):
            self.assertNotIn(requests.classify(text,{'history':[]},bot.now())['action'],('request','retry','confirm'))

    def test_model_supplement_requires_verbatim_current_instruction(self):
        good='可以让我欣赏一幅你的近照吗'
        value={'image_request':{'action':'request','evidence':good}}
        self.assertTrue(requests.model_request(good,value))
        for text in ('我今天去散步','不要自拍',good+'不用画了'):
            data=copy.deepcopy(value)
            if text.endswith('不用画了'):data['image_request']['evidence']='不用画了'
            self.assertFalse(requests.model_request(text,data))

    def test_unaccepted_candidate_never_allows_progress_or_promises(self):
        for intent in ('image_request','casual_chat','share'):
            for text in ('我画一张给你。','行，再来一次','落地窗那张在弄。别催。','催也没用，出得慢。','行，我换一下。'):
                plan={'intent':intent,'segments':[text]}
                guarded=progress.guard_blocked_reply(plan,{'_candidate_scheduled':True,'_visual_context':True})
                self.assertEqual(guarded['segments'],['这次没有开始出图。'])

    def test_planning_receipt_cannot_claim_generation(self):
        result=progress.guard_blocked_reply({'segments':['在画了，别催。']},{'_drawing_started':True,'_image_receipt':{'status':'planning','text':'还在准备画面，没开始出图。'}})
        self.assertIn('没开始出图',result['segments'][0])

class SceneTests(unittest.TestCase):
    def test_new_home_subscene_replaces_cached_desk(self):
        s={'state':copy.deepcopy(bot.DEFAULT)}
        data={'state':copy.deepcopy(s['state']),'visual_tags':'','visual_scene':dict(identity='woman',outfit='dress',setting='home room, desk by window',action='reading',composition='medium shot')}
        assemble(s,data)
        data['visual_scene']['setting']='home genkan entryway, front door, shoe cabinet'
        complete_from_cache(s,data)
        tags=assemble(s,data)
        self.assertIn('shoe cabinet',tags);self.assertNotIn('desk by window',tags)
        self.assertEqual(s['visual_cache']['setting']['tags'],data['visual_scene']['setting'])

    def test_environment_and_action_are_single_prompt_and_qa_target(self):
        from test_planning_fixture import creative
        for environment in ('home genkan entryway, front door, shoe cabinet','home kitchen, stove, soup pot','residential street, sidewalk, street lamps'):
            data={'creative_brief':{**creative('holding a spoon'),'gesture':'holding a spoon','environment':environment},'visual_scene':{'setting':'home room, desk by window'}}
            self.assertTrue(image_concept.apply(data));self.assertEqual(data['visual_scene']['setting'],environment)
            self.assertEqual(data['image_plan']['action'],'holding a spoon')
            self.assertEqual(image_concept.conflicts(data),[])

    def test_conflicts_are_caught_before_gpu(self):
        self.assertEqual(image_concept.conflicts({'visual_scene':{'setting':'outdoors street, indoor kitchen'},'image_plan':{'action':'standing, sitting'}}),['indoor_outdoor_conflict','standing_sitting_conflict'])

    def test_clock_correction_preserves_place_nouns_and_no_stale_light(self):
        tags=bot.scene_tags('home kitchen in the evening, warm afternoon sunlight',23,'')
        self.assertIn('home kitchen',tags);self.assertIn('night',tags)
        self.assertNotIn('evening',tags);self.assertNotIn('afternoon',tags)

    def test_saved_indoor_location_cannot_become_a_street_without_transition(self):
        data={'state':{'location':'家里'},'visual_scene':{'setting':'residential street, sidewalk'},'image_plan':{'action':'walking'}}
        self.assertIn('saved_location_conflict',image_concept.conflicts(data))

class DiscussionMemoryTests(unittest.TestCase):
    def test_cross_day_and_natural_visual_switch_suspend_old_topic(self):
        current=bot.now();s={}
        for _ in range(4):
            d=depth.plan(s,'今天继续学习物理课程',[],current);depth.finish(s,'今天继续学习物理课程','嗯，这一步可以接着推。',{},d,current)
        for text,at in (('今天在图书馆写英语卷子',current+timedelta(days=2)),(SELFIE,current)):
            d=depth.plan(s,text,[],at)
            self.assertEqual(d['relation'],'switch');self.assertEqual(d['stage'],'A')
            self.assertEqual(d['state']['substantive_turns'],0);self.assertTrue(d['state']['suspended_topics'])

    def test_missing_metadata_is_observable_and_bounded(self):
        data=response_plan.normalize({'reply':'好。'},bot.DEFAULT,'我喜欢清茶')
        self.assertEqual(data['_memory_extraction']['status'],'missing')
        data=response_plan.normalize({'reply':'好。','memory_updates':[]},bot.DEFAULT,'我喜欢清茶')
        self.assertEqual(data['_memory_extraction']['status'],'evaluated')
        s={};current=bot.now()
        for i in range(14):
            d=depth.plan(s,'这段人物关系需要记录下来。',[],current)
            depth.finish(s,'这段人物关系需要记录下来。','嗯，这样能接得上。',{},d,current)
        self.assertLess(s['dialogue_state']['substantive_turns'],12)
        self.assertEqual(s['dialogue_state']['dialogue_update_status'],'missing')

    def test_fact_record_restart_recall_correct_forget_and_rejection_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder);current=bot.now();diag={}
            life.record('u',[{'kind':'preference','value':'喜欢清茶','evidence':'我喜欢清茶'},
                             {'kind':'location','value':'上海','evidence':'用户没说过'}],'我喜欢清茶',current,diag)
            self.assertEqual((diag['accepted'],diag['rejected']),(1,1))
            self.assertNotIn('清茶',json.dumps(diag,ensure_ascii=False))
            life=Life(folder);self.assertIn('喜欢清茶',life.recall('u','你记得我的口味吗',current))
            self.assertEqual(life.facts('other'),[])
            self.assertTrue(life.correct('u','喜欢清茶','喜欢红茶',current))
            self.assertEqual(life.forget('u','喜欢红茶'),1)
            self.assertEqual(Life(folder).facts('u'),[])

class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data
    async def finish(self):
        while any(not task.done() for task in self.instance.tasks):await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
    def texts(self):return [p[1]['message'][0]['data'].get('text','') for p in self.packets]

    async def test_real_selfie_sequence_retains_constraints_and_does_not_duplicate_confirmation(self):
        started=asyncio.Event();release=asyncio.Event()
        async def draw(*args,**kwargs):started.set();await release.wait();return b'new-selfie'
        self.instance.comfy.image=AsyncMock(side_effect=draw)
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())) as model,patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,SELFIE);await asyncio.wait_for(started.wait(),1)
            s=self.instance.store.get(self.key);s['history']=s['history']+([{'role':'user','content':'顺便说一下今天挺有趣。'}]*10);self.instance.store.save(self.key,s)
            await self.instance.handle(self.event,self.key,'好了没')
            await self.instance.handle(self.event,self.key,'去画吧')
            self.assertEqual(model.await_count,1)
            self.assertEqual(len(list((self.root/'data/image_jobs').glob('*/job.json'))),1)
            self.assertNotIn('没有开始出图',self.texts()[-1])
            release.set();await self.finish()
        s=self.instance.store.get(self.key)
        self.assertEqual(requests.latest(s)['request'],SELFIE)
        job=json.loads(next((self.root/'data/image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['source_message'],SELFIE);self.assertEqual(job['status'],'completed')

    async def test_older_completed_picture_never_satisfies_a_new_selfie(self):
        job=bot.image_director.Job(self.root/'data/image_jobs',{}, {'image_task_id':'b'*32},'',0)
        job.data['conversation_id']=hashlib.sha256(self.key.encode()).hexdigest()[:12];job.record('completed')
        s=self.instance.store.get(self.key);s['last_image_job_id']=job.id
        requests.receive(s,{'action':'request','text':SELFIE,'evidence':SELFIE},bot.now(),None)
        self.instance.store.save(self.key,s)
        with patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.handle(self.event,self.key,'好了没')
        model.assert_not_called();self.assertIn('没有开始出图',self.texts()[0])

    async def test_original_selfie_outfit_and_window_constraints_survive_transition_and_cache(self):
        s=self.instance.store.get(self.key);cached=self.image_data();assemble(s,cached);self.instance.store.save(self.key,s)
        data=self.image_data();data['state'].update(clothing='淡蓝色长裙、拖鞋和踩脚袜',location='房间落地窗前')
        data['visual_scene'].update(outfit='light blue long dress, slippers, stirrup stockings',setting='room, floor-to-ceiling window')
        self.instance.comfy.image=AsyncMock(return_value=b'new-outfit')
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,SELFIE);await self.finish()
        job=json.loads(next((self.root/'data/image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['snapshot']['outfit'],'淡蓝色长裙、拖鞋和踩脚袜')
        self.assertIn('light blue long dress',job['prompt']);self.assertIn('floor-to-ceiling window',job['prompt'])
        self.assertFalse(bot.image_director.user_transition('clothing','我穿着蓝裙，你觉得怎么样？'))

    async def test_rejected_background_candidate_has_no_started_flag_or_delivery_promise(self):
        import image_candidates
        s=self.instance.store.get(self.key)
        candidate=image_candidates.make('life',self.key,'reading','activity','在房间读书',s,bot.now())
        data=self.image_data();data['reply']='我画一张给你。';data['response_plan']={'intent':'image_request','mode':'text','segments':['我画一张给你。']}
        real_decide=bot.image_director.decide
        def reject(*args,**kwargs):return {'allowed':False,'kind':'life','reason':'event_already_attempted'}
        with patch.dict('os.environ',{'IMAGE_MODE':'auto'}),patch.object(image_candidates,'conversation',return_value=candidate),patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(bot.image_director,'decide',side_effect=reject):
            await self.instance.handle(self.event,self.key,'今天读到这里。');await self.finish()
        self.assertFalse(data.get('_drawing_started'));self.assertTrue(data.get('_candidate_scheduled'))
        self.assertEqual(self.texts(),['这次没有开始出图。'])
        self.assertFalse(list((self.root/'data/image_jobs').glob('*/job.json')))

    async def test_commands_and_unbound_confirmation_never_call_model(self):
        with patch.object(bot,'deepseek',AsyncMock()) as model:
            for text in ('/信息','/不存在','去画吧','/进度'):await self.instance.handle(self.event,self.key,text)
        model.assert_not_called();self.assertIn('角色',self.texts()[0]);self.assertIn('/帮助',self.texts()[1])

    async def test_model_evidence_upgrade_is_explicit_with_auto_disabled(self):
        text='可以让我欣赏一幅你的近照吗'
        data=self.image_data();data['image_request']={'action':'request','evidence':text}
        self.instance.comfy.image=AsyncMock(return_value=b'image')
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,text);await self.finish()
        job=json.loads(next((self.root/'data/image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['decision']['kind'],'explicit');self.assertEqual(job['status'],'completed')

    async def test_waiting_message_owns_clock_and_does_not_reuse_last_reply(self):
        from reply_scheduler import CURRENT
        ticket=self.instance.scheduler.begin(self.key,5);ticket.started-=20000
        future=asyncio.get_running_loop().create_future()
        seen=[]
        async def send(*args,**kwargs):
            seen.append((CURRENT.get(),kwargs));return {'message_id':1}
        cfg={**bot.preferences.load(),'image_chat':True,'image_chat_max':3}
        with patch.object(bot.preferences,'load',return_value=cfg),patch.object(bot.asyncio,'wait_for',AsyncMock(side_effect=asyncio.TimeoutError)),patch.object(self.instance,'send',side_effect=send):
            await self.instance.image_chatter({**self.event,'_image_task_id':'a'*32},self.key,['安静翻到这一页。','第二句'],self.instance.activity.get(self.key,0),future)
        future.set_result(None)
        self.assertEqual(len(seen),1);self.assertIsNone(seen[0][0]);self.assertTrue(seen[0][1]['background'])
        records=[json.loads(line) for line in (self.root/'data/delivery_metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        record=records[-1];self.assertEqual(record['origin'],'image_waiting');self.assertEqual(record['image_task_id'],'a'*32)
        self.assertLess(record['elapsed_seconds'],2);self.assertEqual(ticket.packets,0)

    async def test_reconnect_grace_does_not_plan_an_unrelated_life_image(self):
        self.instance.connected_at=time.monotonic();self.instance.users={42}
        cfg={**bot.preferences.load(),'proactive_enabled':True}
        with patch.dict('os.environ',{'IMAGE_MODE':'auto'}),patch.object(bot.preferences,'load',return_value=cfg),patch.object(self.instance,'prepare_candidate',AsyncMock()) as plan:
            await self.instance.initiative_tick()
        plan.assert_not_called()

    def recovered(self):
        job=bot.image_director.Job(self.root/'data/image_jobs',{}, {'kind':'explicit'},'',0)
        job.data['conversation_id']=hashlib.sha256(self.key.encode()).hexdigest()[:12]
        job.record('recovered_output',recovered_image='recovered.png')
        (job.path/'recovered.png').write_bytes(b'recovered-pixels')
        s=self.instance.store.get(self.key);requests.receive(s,{'action':'request','text':SELFIE,'evidence':SELFIE},bot.now(),job.id)
        self.instance.store.save(self.key,s);return job

    async def test_recovered_original_is_checked_and_delivered_only_on_command(self):
        job=self.recovered();self.instance.comfy.image=AsyncMock()
        with patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})) as qa,patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.instance.handle(self.event,self.key,'/取图')
            await self.instance.handle(self.event,self.key,'/取图')
        self.assertEqual(qa.await_count,1);model.assert_not_called();self.instance.comfy.image.assert_not_called()
        self.assertEqual(sum(p[1]['message'][0]['type']=='image' for p in self.packets),1)
        self.assertEqual(json.loads((job.path/'job.json').read_text(encoding='utf-8'))['status'],'completed')

    async def test_unknown_recovered_qq_ack_is_never_automatically_resent(self):
        job=self.recovered();original=self.instance.call
        async def call(action,params,ws):
            if params['message'][0]['type']=='image':self.packets.append((action,params));return {}
            return await original(action,params,ws)
        self.instance.call=call
        with patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})) as qa:
            await self.instance.handle(self.event,self.key,'/取图')
            await self.instance.handle(self.event,self.key,'/取图')
        self.assertEqual(qa.await_count,1)
        self.assertEqual(sum(p[1]['message'][0]['type']=='image' for p in self.packets),1)
        self.assertEqual(json.loads((job.path/'job.json').read_text(encoding='utf-8'))['status'],'qq_result_unknown')

class Response:
    def __init__(self,value):self.value=value
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def raise_for_status(self):pass
    async def json(self):return self.value
    async def read(self):return self.value

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.job=bot.image_director.Job(self.root/'data/image_jobs',{}, {'kind':'explicit'},'',0)
        self.job.data['created_at']=time.time()-100
        self.job.record('submitted',prompt_id='original-id')
        self.routes=[];self.history={};self.queue={'queue_running':[],'queue_pending':[]}
        buffer=io.BytesIO();Image.new('RGB',(256,256),'blue').save(buffer,format='PNG');self.pixels=buffer.getvalue()
        parent=self
        class HTTP:
            def get(self,url,**kwargs):
                parent.routes.append(url)
                return Response(parent.pixels if url.endswith('/view') else parent.queue if url.endswith('/queue') else parent.history)
            def post(self,*args,**kwargs):raise AssertionError('Recovery must never POST')
        self.http=HTTP()
    async def asyncTearDown(self):self.temp.cleanup()
    async def reconcile(self):
        await image_recovery.reconcile(self.http,'http://mock',self.root,time.time())
        return json.loads((self.job.path/'job.json').read_text(encoding='utf-8'))
    async def test_finished_original_prompt_is_saved_without_resubmission_or_qq(self):
        self.history={'original-id':{'status':{'completed':True},'outputs':{'9':{'images':[{'filename':'output.png','type':'output'}]}}}}
        result=await self.reconcile();self.assertEqual(result['status'],'recovered_output')
        self.assertTrue((self.job.path/'recovered.png').is_file());self.assertFalse(result['automatic_resend'])
        before=len(self.routes);await self.reconcile();self.assertEqual(len(self.routes),before)
    async def test_queued_original_is_tracked_and_missing_history_is_unknown(self):
        self.queue['queue_running']=[[0,'original-id']]
        self.assertEqual((await self.reconcile())['status'],'reconciling')
        self.queue['queue_running']=[]
        self.assertEqual((await self.reconcile())['status'],'outcome_unknown')
    async def test_dispatch_unknown_and_known_ack_do_not_resend(self):
        self.job.record('sending')
        self.assertEqual((await self.reconcile())['status'],'qq_result_unknown');self.assertEqual(self.routes,[])
        self.job.record('sending',qq_result={'message_id':123})
        self.assertEqual((await self.reconcile())['status'],'completed');self.assertEqual(self.routes,[])
    async def test_pre_submission_restart_and_stale_auto_terminate(self):
        self.job.data.pop('prompt_id');self.job.record('planning')
        self.assertEqual((await self.reconcile())['reason'],'restart_before_gpu_submission')
        self.job.data['decision']['kind']='life';self.job.data['created_at']=time.time()-10000
        self.job.record('submitted',prompt_id='original-id')
        self.assertEqual((await self.reconcile())['status'],'expired');self.assertEqual(self.routes,[])

if __name__=='__main__':unittest.main()
