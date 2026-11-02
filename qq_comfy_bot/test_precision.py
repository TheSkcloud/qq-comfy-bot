import asyncio,copy,io,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch,AsyncMock,Mock
from aiohttp import web
from PIL import Image
import aiohttp,bot,image_candidates as c,image_director as d,social_reasoning as social
import test_remediation
from test_bot import workflow_info

class SocialTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.s={'state':copy.deepcopy(bot.DEFAULT),'history':[]}
    def test_assistant_joke_does_not_become_user_comic_event(self):
        data={'response_plan':{'intent':'teasing'},'assistant_tone':'teasing','user_intent':'question','trigger_actor':'assistant','image_value':{'suitable':True,'kind':'comic','reason':'有点好玩'}}
        self.assertIsNone(c.conversation('k',self.s,'连锁店不同城市味道一样吗',data,self.now))
    def test_forged_user_evidence_is_rejected(self):
        data={'user_intent':'teasing','trigger_actor':'user','trigger_evidence':'哈哈学你说话','interaction_event':{'type':'shared_joke'},'image_value':{'suitable':True,'kind':'comic','reason':'接梗'}}
        self.assertIsNone(c.conversation('k',self.s,'这家店味道怎么样',data,self.now))
    def test_negative_emotion_alone_does_not_require_image(self):
        data=social.normalize({'response_plan':{'intent':'emotion'},'affect':{'stress':1}},'今天有点累',[])
        self.assertEqual(data['emotion_state']['category'],'fatigue')
        self.assertIsNone(c.conversation('k',self.s,'今天有点累',data,self.now))
    def test_refusal_persists_until_explicit_reopen(self):
        self.assertTrue(social.boundary_update(self.s,'项目进展的事别提了，今天不想讨论',self.now))
        self.assertTrue(social.boundary_blocks(self.s,'项目进展不顺利',self.now))
        social.boundary_update(self.s,'我在吃晚饭',self.now)
        self.assertIn('topic_boundary',self.s)
        social.boundary_update(self.s,'还是聊聊项目进展吧',self.now)
        self.assertNotIn('topic_boundary',self.s)
    def test_generic_refusal_does_not_turn_personal_pronoun_into_banned_topic(self):
        self.s['history']=[{'role':'user','content':'项目的测试最近一直报错'}]
        social.boundary_update(self.s,'我不想聊这件事',self.now)
        self.assertTrue(social.boundary_blocks(self.s,'项目测试还是报错',self.now))
        self.assertFalse(social.boundary_blocks(self.s,'我买了一个新杯子',self.now))

    def test_refusal_does_not_block_unrelated_activity(self):
        social.boundary_update(self.s,'实验室的事别提了',self.now)
        self.assertFalse(social.boundary_blocks(self.s,'在房间读书',self.now))
    def test_ordinary_inability_is_not_a_topic_refusal(self):
        self.assertFalse(social.boundary_update(self.s,'我不会说法语',self.now))
    def test_rhetorical_unfairness_is_not_automatic_technical_advice(self):
        import speech
        self.assertEqual(speech.plan('我没选上，可为什么别人就有机会？',[])['mode'],'vent')
        self.assertEqual(speech.plan('我没选上，帮我分析怎么改申请材料',[])['mode'],'help')
    def test_self_comparison_needs_negative_evidence(self):
        self.assertEqual(social.local_emotion('也没好多少，我自己也是个打杂的',[]),'self_deprecation')
        self.assertEqual(social.local_emotion('我自己也是个读书爱好者',[]),'neutral')

    def test_implicit_setback_is_recognized_without_forcing_comfort(self):
        result=social.normalize({'response_plan':{'intent':'acknowledge'}},'练了那么久，最后还是没选上',[])
        self.assertEqual(result['emotion_state']['category'],'disappointment')
        self.assertFalse(result['image_value']['suitable'])
    def test_self_deprecation_keeps_evidenced_short_context(self):
        history=[{'role':'user','content':'我还是那个垫底的。'}]
        result=social.normalize({'emotion_state':{'category':'self_deprecation','intensity':.65,'evidence':'我还是那个垫底的。'}},'又轮到我垫底了',history)
        social.remember(self.s,result,self.now)
        self.assertEqual(self.s['social_state'][-1]['category'],'self_deprecation')
        self.assertIn('垫底',social.context(self.s))

class GroundingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_cause_uses_one_small_repair(self):
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def raise_for_status(self):pass
            async def json(self):return {'choices':[{'message':{'content':json.dumps({'reply':'准备了那么久却没选上，确实让人失望。原因我不知道。'})}}]}
        provider=type('Provider',(),{})();provider.post=Mock(return_value=Response())
        data={'reply':'导师资源可能不一样。','response_plan':{'segments':['导师资源可能不一样。']}}
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'local'}):
            result=await bot.repair_grounding(provider,data,'为什么别人有机会，我却没选上',[],'http://local', ['unsupported cause'])
        self.assertEqual(provider.post.call_count,1)
        self.assertNotIn('导师',result['reply'])
        self.assertTrue(result['_grounding_repair']['applied'])
    async def test_grounded_emotion_does_not_need_repair(self):
        self.assertFalse(social.grounding_issues('等了很久却没选上，会失望也正常。','我没选上',[]))
        self.assertTrue(social.grounding_issues('导师可能没给你资源','我没选上',[]))


class BoundaryEntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_remediation.PipelineTests.asyncSetUp
    asyncTearDown=test_remediation.PipelineTests.asyncTearDown
    async def test_refusal_at_enqueue_never_reopens_topic_or_calls_model(self):
        with patch.object(bot,'deepseek',AsyncMock()) as api:
            self.instance.enqueue(self.event,self.key,'实验室的事别提了，东西故障了')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        api.assert_not_called()
        self.assertIn('topic_boundary',self.instance.store.get(self.key))
        self.assertEqual(len(self.packets),1)
        self.assertNotIn('?',self.packets[0][1]['message'][0]['data']['text'])
        self.assertNotIn('？',self.packets[0][1]['message'][0]['data']['text'])

class BackgroundClosedLoop(unittest.IsolatedAsyncioTestCase):
    async def test_virtual_clock_real_routine_to_http_comfy_qa_and_websocket_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);root.joinpath('workflow.json').write_text((bot.ROOT/'workflow.json').read_text(encoding='utf-8-sig'),encoding='utf-8')
            info=workflow_info(json.loads((root/'workflow.json').read_text()))
            clock=[datetime(2026,11,1,13,59,tzinfo=ZoneInfo('Asia/Shanghai'))]
            state=copy.deepcopy(bot.DEFAULT);state.update(location='房间书桌旁',clothing='白衬衫')
            output=io.BytesIO();Image.effect_noise((256,256),50).convert('RGB').save(output,format='PNG')
            counts={'prompts':0,'plans':0,'qa':0,'messages':0,'inbound':0};sent=asyncio.Event()
            async def stats(r):return web.json_response({'system':{}})
            async def objects(r):return web.json_response(info)
            async def prompt(r):counts['prompts']+=1;return web.json_response({'prompt_id':'virtual-life','node_errors':{}})
            async def history(r):return web.json_response({'virtual-life':{'outputs':{'9':{'images':[{'filename':'virtual.png','subfolder':'','type':'output'}]}},'status':{'completed':True}}})
            async def view(r):return web.Response(body=output.getvalue(),content_type='image/png')
            async def chat(r):
                body=await r.json()
                if '生成图验收员' in body['messages'][0]['content']:
                    counts['qa']+=1;data={k:True for k in ('pass','identity_ok','scene_ok','anatomy_ok','style_ok')};data['uncertain']=False
                else:
                    counts['plans']+=1;data={'reply':'读到一半。','state':state,'send_image':True,'visual_tags':'woman, reading','visual_scene':{'identity':'adult woman','outfit':'white shirt','setting':'room desk','action':'reading a book','composition':'side view'},'image_caption':'翻到这页了。'}
                if counts['plans']:
                    from test_planning_fixture import creative
                    data['creative_brief']=creative()
                return web.json_response({'choices':[{'message':{'content':json.dumps(data)},'finish_reason':'stop'}]})
            async def ws(r):
                conn=web.WebSocketResponse();await conn.prepare(r)
                async for message in conn:
                    packet=json.loads(message.data)
                    if packet['action'].startswith('send_'):
                        counts['messages']+=1;sent.set()
                    await conn.send_json({'echo':packet['echo'],'retcode':0,'data':{'message_id':321}})
                return conn
            app=web.Application();app.router.add_get('/system_stats',stats);app.router.add_get('/object_info',objects);app.router.add_post('/prompt',prompt);app.router.add_get('/history/{pid}',history);app.router.add_get('/view',view);app.router.add_post('/chat/completions',chat);app.router.add_get('/ws',ws)
            runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1];url=f'http://127.0.0.1:{port}'
            cfg={**bot.preferences.load(),'proactive_enabled':True,'drawing':d.config({'initiative':0,'daily_budget':4})}
            instance=None;network=None;loop=None
            try:
                with patch.object(bot,'ROOT',root),patch.object(bot,'now',side_effect=lambda:clock[0]),patch.object(bot.preferences,'load',return_value=cfg),patch.dict(os.environ,{'COMFYUI_URL':url,'NAPCAT_WS_URL':url.replace('http','ws')+'/ws','NAPCAT_TOKEN':'local','DEEPSEEK_BASE_URL':url,'DEEPSEEK_API_KEY':'local','ALLOWED_USERS':'42','IMAGE_MODE':'auto'}):
                    async with aiohttp.ClientSession() as http:
                        instance=bot.Bot(http);key='private:0:42'
                        instance.store.save(key,{'state':state,'history':[],'last_user_at':clock[0].isoformat()})
                        # Stored schedule is consumed by the real current_event; no candidate fixture/mocked launcher.
                        with sqlite3.connect(instance.context.routine.path) as db:
                            db.execute('INSERT INTO routines VALUES (?,?,?)',('2026-11-01','42:'+bot.character_key(state['character']),json.dumps([(0,14,'休息'),(14,24,'在房间读书')])))
                        db.close()
                        network=asyncio.create_task(instance.run());await asyncio.wait_for(instance.connected.wait(),2)
                        advances=0
                        async def virtual_sleep(seconds):
                            nonlocal advances
                            advances+=1
                            if advances==1:
                                clock[0]+=timedelta(minutes=32);await asyncio.sleep(0)
                            else:await asyncio.Event().wait()
                        loop=asyncio.create_task(bot.initiative_loop(instance,sleep=virtual_sleep))
                        await asyncio.wait_for(sent.wait(),8)
                        if instance.image_tasks:await asyncio.wait_for(asyncio.gather(*list(instance.image_tasks.values())),2)
                        live=instance.store.get(key)
                        self.assertEqual([m['role'] for m in live['history']],['assistant'])
                        self.assertIn('图片已实际发送成功',live['history'][0]['content'])
                        self.assertEqual(counts,{'prompts':1,'plans':1,'qa':1,'messages':1,'inbound':0})
                        clock[0]+=timedelta(minutes=5);await instance.initiative_tick()
                        self.assertEqual(counts['prompts'],1)
                        jobs=list((root/'data'/'image_jobs').glob('*/job.json'))
                        self.assertEqual(json.loads(jobs[0].read_text(encoding='utf-8'))['lifecycle'],'sent')
                        loop.cancel();network.cancel();await asyncio.gather(loop,network,return_exceptions=True)
            finally:
                for task in (loop,network):
                    if task and not task.done():task.cancel()
                await asyncio.gather(*[t for t in (loop,network) if t],return_exceptions=True)
                if instance:instance.store.db.close()
                await runner.cleanup()

if __name__=='__main__':unittest.main()

