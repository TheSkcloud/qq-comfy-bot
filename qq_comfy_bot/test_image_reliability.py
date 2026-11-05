import asyncio, copy, io, json, os, tempfile, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import aiohttp
from aiohttp import web
from PIL import Image
import bot, image_director as d, image_workflow, shot_director as shots
from visual_brief import assemble, complete_from_cache
import test_upgrade


class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data

    async def finish(self):
        while any(not t.done() for t in self.instance.tasks):
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)

    async def test_tags_only_gets_one_independent_plan_and_success(self):
        data=self.image_data();data['visual_scene']={}
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)) as chat,patch('image_planner.complete_visual',AsyncMock(return_value=self.image_data())) as repair,patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'画一张')
            await self.finish()
        self.assertEqual(chat.await_count,1);self.assertEqual(repair.await_count,1)
        jobs=list((self.root/'data'/'image_jobs').glob('*/job.json'))
        job=json.loads(jobs[0].read_text(encoding='utf-8'))
        self.assertEqual(job['lifecycle'],'sent');self.assertIn('shot_plan',job['snapshot'])
        self.assertEqual(len(self.instance.store.get(self.key)['shot_history']),1)
        self.assertEqual(job['events'][0]['status'],'received')

    async def test_repair_failure_keeps_chat_and_records_failure(self):
        data=copy.deepcopy(self.data);data['visual_tags']='woman'
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)) as chat,patch('image_planner.complete_visual',AsyncMock(side_effect=ValueError('missing outfit'))) as repair:
            await self.instance.handle(self.event,self.key,'画一张')
            await self.finish()
        self.assertEqual(chat.await_count,1);self.assertEqual(repair.await_count,1)
        texts=[p[1]['message'][0]['data'].get('text','') for p in self.packets]
        self.assertIn('最新回复',texts);self.assertTrue(any('没有开始生成' in s for s in texts))
        job=json.loads(next((self.root/'data'/'image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['reason'],'visual_planning_failed')
        self.assertNotEqual(job['reason'],'queue_full')

    async def test_matching_cache_avoids_repair_but_changed_outfit_does_not(self):
        session=self.instance.store.get(self.key);base=self.image_data();assemble(session,base)
        data=self.image_data();data['visual_scene']={'action':'reading a book','composition':'side view'}
        self.assertFalse(complete_from_cache(session,data))
        data['state']['clothing']='new dress';data['visual_scene']['outfit']=''
        self.assertIn('outfit',complete_from_cache(session,data))

    async def test_invalid_old_chinese_cache_cannot_overwrite_visual_repair(self):
        session=self.instance.store.get(self.key);data=self.image_data();assemble(session,data)
        session['visual_cache']['identity_tags']='蓝紫发金眼'
        session['visual_cache']['outfit']['tags']='白衬衫'
        session['visual_cache']['setting']['tags']='房间'
        self.assertFalse(complete_from_cache(session,data))
        assemble(session,data)
        self.assertEqual(session['visual_cache']['outfit']['tags'],'home clothing')

    async def test_chat_during_visual_repair_and_cancel_prevents_late_image(self):
        started=asyncio.Event();release=asyncio.Event()
        async def repair(*args):started.set();await release.wait();return self.image_data()
        image=self.image_data();image['visual_scene']={}
        async def chat(*args,**kwargs):return image if args[3]=='画一张' else copy.deepcopy(self.data)
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',chat),patch('image_planner.complete_visual',repair):
            await self.instance.handle(self.event,self.key,'画一张')
            await asyncio.wait_for(started.wait(),1)
            await asyncio.wait_for(self.instance.handle(self.event,self.key,'另外聊聊今天的事'),1)
            self.assertGreaterEqual(len(self.packets),2)
            self.instance.enqueue(self.event,self.key,'取消画图')
            release.set();await self.finish()
        self.instance.comfy.image.assert_not_called()
        job=json.loads(next((self.root/'data'/'image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['status'],'cancelled')

    async def test_explicit_bypasses_auto_budgets_and_disabled_auto(self):
        session=self.instance.store.get(self.key)
        session['drawing_sends']=[{'kind':'life','day':bot.now().date().isoformat(),'at':bot.now().isoformat()}]*50
        self.instance.store.save(self.key,session)
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'画一张')
            await self.finish()
        self.assertEqual(self.instance.comfy.image.await_count,1)

    async def test_rapid_explicit_requests_are_not_merged(self):
        self.instance.handle=AsyncMock(return_value=True)
        self.instance.warm_image_engine=AsyncMock()
        self.instance.enqueue(self.event,self.key,'画一张看书')
        self.instance.enqueue(self.event,self.key,'再画一张侧脸')
        await self.finish()
        self.assertEqual([c.args[2] for c in self.instance.handle.await_args_list],['画一张看书','再画一张侧脸'])

    async def test_invalid_main_model_has_received_task_and_failure(self):
        with patch.object(bot,'deepseek',AsyncMock(side_effect=ValueError('bad JSON'))):
            await self.instance.handle(self.event,self.key,'画一张')
        job=json.loads(next((self.root/'data'/'image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['reason'],'chat_model_or_context_failed')

    async def test_missing_qq_image_ack_is_unknown_and_never_retried(self):
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        sent=[]
        async def call(action,params,ws):
            sent.append(params)
            return {} if any(s['type']=='image' for s in params.get('message',[])) else {'message_id':1}
        self.instance.call=call
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'画一张')
            await self.finish()
        job=json.loads(next((self.root/'data'/'image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['lifecycle'],'outcome_unknown');self.assertFalse(job['automatic_resend'])
        self.assertEqual(sum(any(s['type']=='image' for s in p['message']) for p in sent),1)
        self.assertFalse(self.instance.store.get(self.key).get('drawing_sends'))

    async def test_qq_rejection_is_distinct_from_unknown_ack(self):
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        async def call(action,params,ws):
            if any(s['type']=='image' for s in params.get('message',[])):raise bot.ImageFailure('qq_send_rejected','send','retcode=1200')
            return {'message_id':1}
        self.instance.call=call
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'画一张')
            await self.finish()
        job=json.loads(next((self.root/'data'/'image_jobs').glob('*/job.json')).read_text(encoding='utf-8'))
        self.assertEqual(job['status'],'failed');self.assertEqual(job['reason'],'qq_send_rejected')

    async def test_two_explicit_requests_reach_two_jobs(self):
        self.instance.warm_image_engine=AsyncMock()
        self.instance.comfy.image=AsyncMock(side_effect=[b'image1',b'image2'])
        with patch.object(bot,'deepseek',AsyncMock(side_effect=[self.image_data(),self.image_data()])),patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张看书')
            self.instance.enqueue(self.event,self.key,'再画一张侧脸')
            await self.finish()
        jobs=[json.loads(p.read_text(encoding='utf-8')) for p in (self.root/'data'/'image_jobs').glob('*/job.json')]
        self.assertEqual(len(jobs),2);self.assertTrue(all(j['lifecycle']=='sent' for j in jobs))

    async def test_cancel_during_automatic_candidate_planning_prevents_launch(self):
        import image_candidates
        started=asyncio.Event();release=asyncio.Event()
        async def model(*args,**kwargs):started.set();await release.wait();return self.image_data()
        session=self.instance.store.get(self.key)
        candidate=image_candidates.make('life',self.key,'reading','test_activity','在房间读书',session,bot.now())
        self.instance.comfy.image=AsyncMock()
        with patch.object(bot,'deepseek',model):
            planning=asyncio.create_task(self.instance.prepare_candidate(self.event,self.key,candidate,bot.preferences.load()))
            await asyncio.wait_for(started.wait(),1)
            self.instance.enqueue(self.event,self.key,'取消画图')
            release.set();await planning;await self.finish()
        self.instance.comfy.image.assert_not_called()


class ShotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot=dict(identity='suzuki hana, golden eyes, long blue-violet hair',action='reading a book',props='book',
                           style='life',camera='medium shot',setting_tags='room, desk',purpose='illustration')

    def test_twenty_four_plans_vary_without_changing_scene(self):
        before=copy.deepcopy(self.snapshot);history=[];ids=[]
        for _ in range(24):
            plan=shots.choose(self.snapshot,'',history);ids.append(plan['id'])
            history=(history+[{'plan':plan,'actual':{}}])[-12:]
        self.assertEqual(self.snapshot,before);self.assertGreaterEqual(len(set(ids)),8)
        self.assertTrue(all(a!=b for a,b in zip(ids,ids[1:])))

    def test_actual_camera_history_overrides_planned_camera(self):
        a=shots.choose(self.snapshot)
        actual=dict(size='extreme_wide',elevation='eye_level',direction='three_quarter',composition='frame')
        b=shots.choose(self.snapshot,'',[{'plan':{'id':'other'},'actual':actual}]*12)
        self.assertNotEqual(a['id'],b['id'])

    def test_user_angles_full_body_and_avatar_take_priority(self):
        plan=shots.choose(self.snapshot,'背影全身，低机位，三分法')
        self.assertEqual((plan['size'],plan['direction'],plan['elevation'],plan['composition']),('full','back','low','thirds'))
        self.assertFalse(plan['expectations']['eyes_expected'])
        avatar=shots.choose({**self.snapshot,'purpose':'avatar','camera':'square avatar, head and shoulders, centered face'})
        self.assertEqual(avatar['tags'],'square avatar, head and shoulders, centered face')

    def test_detail_back_and_wide_have_suitable_visibility_expectations(self):
        for request in ('手部特写','背影','远景'):
            plan=shots.choose(self.snapshot,request)
            self.assertFalse(plan['expectations']['face_expected']);self.assertFalse(plan['expectations']['eyes_expected'])
        self.assertEqual(shots.choose(self.snapshot,'手部特写')['expectations']['subject'],'hands and activity props')

    def test_comic_stylization_does_not_leak_into_life(self):
        self.assertEqual(shots.choose(self.snapshot)['stylization'],'natural')
        self.assertNotEqual(shots.choose({**self.snapshot,'style':'comic'})['stylization'],'natural')

    def test_shot_dimensions_preserve_sampler_parameters(self):
        snapshot={**self.snapshot,'character_id':'x','outfit':'shirt','hairstyle':'long hair'}
        snapshot['shot_plan']=shots.choose(snapshot,'远景')
        profile=image_workflow.prepare(Path(__file__).parent,snapshot)
        self.assertGreater(profile['width'],profile['height'])
        self.assertEqual(profile['steps'],24);self.assertEqual(profile['cfg'],5.)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patch=patch.object(bot,'ROOT',self.root);self.patch.start()
        self.posts=0;self.downloads=0;self.mode='download';self.runner=None
        async def prompt(request):
            self.posts+=1
            if self.mode=='ambiguous':request.transport.close();return web.Response()
            if self.mode=='reject':return web.json_response({'node_errors':{'70':'missing model'}},status=400)
            return web.json_response({'prompt_id':'same-prompt'})
        async def history(request):
            if self.mode=='timeout':return web.json_response({})
            if self.mode=='gpu':return web.json_response({'same-prompt':{'status':{'status_str':'error','messages':['GPU failed']}}})
            return web.json_response({'same-prompt':{'outputs':{'9':{'images':[{'filename':'image.png','type':'output','subfolder':''}]}},'status':{'completed':True}}})
        async def download(request):
            self.downloads+=1
            if self.mode=='download' and self.downloads==1:return web.Response(status=503)
            return web.Response(body=b'pixels')
        app=web.Application();app.router.add_post('/prompt',prompt);app.router.add_get('/history/{pid}',history);app.router.add_get('/view',download)
        self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.http=aiohttp.ClientSession();self.comfy=bot.Comfy(self.http)
        self.comfy.url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        self.comfy.template={'67':{'inputs':{'text':''}},'70':{'inputs':{'seed':0}}}
        self.job=d.Job(self.root/'jobs',{}, {'priority':0},'prompt',0)
        self.ensure=patch.object(bot.services,'ensure_comfy',AsyncMock());self.ensure.start()

    async def asyncTearDown(self):
        self.ensure.stop();await self.http.close();await self.runner.cleanup();self.patch.stop();self.temp.cleanup()

    async def test_download_recovery_reuses_prompt_without_second_gpu_submission(self):
        with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'download_failed');self.assertTrue(error.exception.retryable)
        self.assertEqual(await self.comfy.image('prompt',self.job),b'pixels')
        self.assertEqual(self.posts,1);self.assertEqual(self.downloads,2)

    async def test_ambiguous_submit_is_not_retryable(self):
        self.mode='ambiguous'
        with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'submission_unknown');self.assertFalse(error.exception.retryable)
        self.assertEqual(self.posts,1)

    async def test_workflow_and_gpu_errors_have_different_failure_codes(self):
        self.mode='reject'
        with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'workflow_rejected')
        self.mode='gpu'
        with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'gpu_execution_failed')

    async def test_service_connection_failure_has_bounded_retry_hint(self):
        with patch.object(bot.services,'ensure_comfy',AsyncMock(side_effect=aiohttp.ClientConnectionError('offline'))):
            with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'service_unavailable');self.assertTrue(error.exception.retryable)

    async def test_generation_deadline_has_a_distinct_nonretryable_code(self):
        self.mode='timeout'
        with patch.dict(os.environ,{'IMAGE_TIMEOUT':'.01'}):
            with self.assertRaises(bot.ImageFailure) as error:await self.comfy.image('prompt',self.job)
        self.assertEqual(error.exception.code,'generation_timeout');self.assertFalse(error.exception.retryable)

    async def test_submission_after_unload_uses_normal_workflow_and_restarts_activity_clock(self):
        self.mode='normal';self.comfy.gpu.state['unloaded']=True
        self.assertEqual(await self.comfy.image('prompt',self.job),b'pixels')
        self.assertEqual(self.posts,1);self.assertFalse(self.comfy.gpu.state['unloaded'])
        self.assertFalse(self.comfy.gpu.state['pending'])


class QATests(unittest.IsolatedAsyncioTestCase):
    def pixels(self):
        buf=io.BytesIO();Image.effect_noise((256,256),50).convert('RGB').save(buf,format='PNG');return buf.getvalue()

    async def test_auto_never_degrades_when_explicit_degradation_is_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            result=await d.inspect_image(None,self.pixels(),{'request_kind':'life'},root,{'qa_daily_budget':0,'qa_exhausted_policy':'pixels_only'})
            self.assertFalse(result['pass']);self.assertFalse(result['degraded'])

    async def test_service_failure_is_withheld_or_explicitly_degraded(self):
        http=Mock();http.post.side_effect=aiohttp.ClientConnectionError('unavailable')
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'}):
            result=await d.inspect_image(http,self.pixels(),{'request_kind':'explicit'},root,{})
            self.assertFalse(result['pass']);self.assertEqual(result['status'],'vision_failed')
            result=await d.inspect_image(http,self.pixels(),{'request_kind':'explicit'},root,{'qa_unavailable_policy':'pixels_only'})
            self.assertTrue(result['pass']);self.assertTrue(result['degraded'])

    def test_generation_planning_and_qa_budgets_are_independent(self):
        cfg=d.config({'daily_budget':1,'planning_daily_budget':99,'qa_daily_budget':240})
        self.assertEqual((cfg['daily_budget'],cfg['planning_daily_budget'],cfg['qa_daily_budget']),(1,99,240))


class LatencyTests(unittest.TestCase):
    def test_only_unambiguous_json_delimiters_can_be_recovered(self):
        import response_plan
        result=response_plan.decode('{"reply":"行。","send_image":false}}',True)
        self.assertEqual(result['reply'],'行。')
        self.assertEqual(result['_model_format'],'trailing_delimiter_recovered')
        with self.assertRaises(ValueError):response_plan.decode('{"reply":"行。"}{"reply":"冲突"}',True)
    def test_slow_model_credits_typing_but_keeps_readable_segment_pacing(self):
        scheduler=bot.ReplyScheduler();scheduler.timing['jitter']=0
        ticket=scheduler.begin('x',30);ticket.started-=40
        quick=scheduler.remaining(ticket,80)
        ticket.model_ms=40000
        self.assertLess(scheduler.remaining(ticket,80),quick*.4)
        self.assertGreater(scheduler.remaining(ticket,80,1),0)


class ContextLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_casual_chat_does_not_fetch_weather_routine_or_notes(self):
        from context_engine import ContextEngine
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'CONTEXT_ENABLED':'1'}):
            engine=ContextEngine(folder)
            engine.weather.ensure=AsyncMock(side_effect=AssertionError('weather on small talk'))
            engine.routine.ensure=AsyncMock(side_effect=AssertionError('routine on small talk'))
            engine.personality=AsyncMock(side_effect=AssertionError('notes on small talk'))
            await engine.build(object(),'42','这个说法挺有意思',bot.now(),True,bot.DEFAULT['character'])
            engine.weather.ensure.assert_not_called();engine.routine.ensure.assert_not_called();engine.personality.assert_not_called()


if __name__=='__main__':unittest.main()
