import asyncio,copy,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image,PngImagePlugin
import aiohttp
import bot,social_reasoning as social,response_plan,image_director as director,image_qa_cache,image_workflow,shot_director
from life import Life

class LanguageTests(unittest.IsolatedAsyncioTestCase):
    def test_achievement_does_not_authorize_an_invented_sleepless_past(self):
        user='我做的海报被选上校展了，这次终于轮到我的作品了！'
        reply='选上了？挺好。之前熬的那些夜总算没白费。'
        self.assertTrue(social.grounding_issues(reply,user,[]))
        self.assertEqual(social.safe_grounded_reply(reply,user,[]),'选上了？挺好。')
        self.assertFalse(social.grounding_issues(reply,user,[{'role':'user','content':'为了这张海报，我前天确实熬了一夜。'}]))
        self.assertTrue(social.grounding_issues(reply,user,[{'role':'assistant','content':'你之前熬夜了。'}]))
    def test_general_advice_hypothesis_and_character_self_report_are_not_user_facts(self):
        for reply in ('如果熬夜了，今天可能会困。','别再熬夜了。','我之前也熬过夜。','选上了，确实不错。'):
            self.assertFalse(social.grounding_issues(reply,'我做的海报被选上了',[]))
    async def test_one_repair_is_checked_and_cannot_change_state_or_memory(self):
        original={'reply':'挺好。之前熬的那些夜总算没白费。','response_plan':{'segments':['draft']},'state':copy.deepcopy(bot.DEFAULT),'memory_updates':[]}
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def raise_for_status(self):pass
            async def json(self):return {'choices':[{'message':{'content':json.dumps({'reply':'你之前通宵也值得了。','state':{'location':'月球'},'memory_updates':[{'value':'bad'}]})}}]}
        http=Mock();http.post.return_value=Response()
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'}):result=await bot.repair_grounding(http,original,'我的海报选上校展了',[],'http://mock',['ungrounded past'])
        self.assertEqual(http.post.call_count,1);self.assertEqual(result['reply'],'挺好。')
        self.assertEqual(result['state'],bot.DEFAULT);self.assertEqual(result['memory_updates'],[])
    async def test_repair_failure_keeps_supported_praise_instead_of_wrong_unknown_cause(self):
        original={'reply':'选上了？挺好。之前熬的那些夜总算没白费。','response_plan':{'segments':['draft']}}
        http=Mock();http.post.side_effect=aiohttp.ClientConnectionError()
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'}):result=await bot.repair_grounding(http,original,'海报选上了',[],'http://mock',['ungrounded past'])
        self.assertEqual(result['reply'],'选上了？挺好。')

class PreferenceTests(unittest.TestCase):
    def test_reported_missing_preference_is_recovered_verbatim_and_recalled(self):
        user='我更喜欢把任务一次做完，不喜欢临时改安排。'
        data=response_plan.normalize({'reply':'按自己的顺序来。','memory_updates':[]},bot.DEFAULT,user)
        self.assertEqual(data['memory_updates'][0]['evidence'],user[:-1]);self.assertEqual(data['_memory_extraction']['local_proposed'],1)
        with tempfile.TemporaryDirectory() as folder:
            life=Life(folder);life.record('u',data['memory_updates'],user,bot.now())
            self.assertIn('不喜欢临时改安排',Life(folder).recall('u','安排',bot.now()))
    def test_model_preference_is_not_duplicated(self):
        item={'kind':'preference','value':'喜欢清茶','evidence':'我喜欢清茶'}
        data=response_plan.normalize({'reply':'嗯。','memory_updates':[item]},bot.DEFAULT,'我喜欢清茶')
        self.assertEqual(data['memory_updates'],[item])
    def test_questions_quotes_jokes_hypotheses_and_recoveries_do_not_make_local_facts(self):
        for user in ('我喜欢这么做吗？','我喜欢这么做吗','假如我喜欢这么做','我喜欢这么做是开玩笑的','我喜欢说“我最聪明”来模仿你','我喜欢这么做是假的'):
            self.assertEqual(response_plan.normalize({'reply':'嗯。'},bot.DEFAULT,user)['memory_updates'],[])
        self.assertEqual(response_plan.normalize({'reply':'嗯。'},bot.DEFAULT,'我喜欢清茶',allow_local_memory=False)['memory_updates'],[])
        self.assertEqual(response_plan.normalize({'reply':'嗯。','_text_only_recovery':True},bot.DEFAULT,'我喜欢清茶')['memory_updates'],[])
        self.assertEqual(response_plan.normalize({'reply':'嗯。','memory_updates':'bad'},bot.DEFAULT,'我喜欢清茶')['memory_updates'],[])

def verdict(passed=True,findings=None):
    return {'pass':passed,'identity_ok':True,'scene_ok':True,'anatomy_ok':True,'style_ok':True,'uncertain':False,
            'findings':findings if findings is not None else [],'issues':[],'visible':'test image','composition':{}}

class QATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        buf=io.BytesIO();Image.effect_noise((256,256),50).convert('RGB').save(buf,format='PNG');self.pixels=buf.getvalue()
        self.snapshot={'identity':'adult woman','outfit':'shirt','setting_tags':'home room','action':'reading','style':'life','request_kind':'explicit','local_time':'2026-11-05T23:00:00+08:00'}
        self.responses=[];self.calls=0;parent=self
        class Response:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def raise_for_status(self):pass
            async def json(self):
                await asyncio.sleep(.01)
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(parent.responses.pop(0))}}]}
        class HTTP:
            def post(self,*args,**kwargs):parent.calls+=1;return Response()
        self.http=HTTP();self.env=patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'});self.env.start()
    async def asyncTearDown(self):self.env.stop();self.temp.cleanup()
    async def check(self,snapshot=None,settings=None):return await director.inspect_image(self.http,self.pixels,snapshot or self.snapshot,self.root,settings or {})
    async def test_same_pixels_and_goal_cannot_be_rerolled_from_fail_to_pass(self):
        self.responses=[verdict(False),verdict(True)]
        first=await self.check();second=await self.check()
        self.assertFalse(first['pass']);self.assertFalse(second['pass']);self.assertTrue(second['cache_hit']);self.assertEqual(self.calls,1)
        self.assertEqual(json.loads((self.root/'visual_qa_budget.json').read_text())['used'],1)
        self.assertEqual(second['usage']['total_tokens'],0)
    async def test_same_image_is_accepted_once_without_more_paid_checks(self):
        self.responses=[verdict()]
        self.assertTrue((await self.check())['pass']);self.assertTrue((await self.check())['cache_hit']);self.assertEqual(self.calls,1)
    async def test_png_metadata_changes_do_not_reroll_identical_pixels(self):
        self.responses=[verdict(False)];await self.check()
        original=self.pixels;buffer=io.BytesIO();metadata=PngImagePlugin.PngInfo();metadata.add_text('job_id','another attempt')
        with Image.open(io.BytesIO(self.pixels)) as pixels:pixels.save(buffer,format='PNG',pnginfo=metadata)
        self.pixels=buffer.getvalue();self.assertNotEqual(self.pixels,original)
        self.assertTrue((await self.check())['cache_hit']);self.assertEqual(self.calls,1)
    async def test_target_changes_invalidate_but_job_ids_and_timestamps_do_not(self):
        self.responses=[verdict(False),verdict(True)]
        await self.check()
        reused={**self.snapshot,'id':'new-job','local_time':'2026-11-06T23:45:00+08:00','revision':4}
        self.assertFalse((await self.check(reused))['pass']);self.assertEqual(self.calls,1)
        changed={**self.snapshot,'action':'drawing'}
        self.assertTrue((await self.check(changed))['pass']);self.assertEqual(self.calls,2)
    async def test_concurrent_duplicate_inspections_share_one_verdict(self):
        self.responses=[verdict(False),verdict(True)]
        results=await asyncio.gather(self.check(),self.check())
        self.assertEqual(self.calls,1);self.assertTrue(all(not result['pass'] for result in results))
    async def test_major_finding_cannot_be_overruled_by_all_true_booleans(self):
        self.responses=[verdict(True,[{'category':'anatomy','severity':'major','evidence':'three visible hands'}])]
        result=await self.check();self.assertFalse(result['pass']);self.assertTrue(result['major_finding_blocked'])
    async def test_missing_findings_are_not_a_valid_semantic_pass(self):
        value=verdict();value.pop('findings');self.responses=[value]
        result=await self.check();self.assertFalse(result['pass']);self.assertEqual(result['status'],'vision_failed')
        self.assertFalse(list((self.root/'image_qa_cache').glob('*.json')))
    async def test_missing_locked_composition_cannot_pass(self):
        self.responses=[verdict()]
        result=await self.check({**self.snapshot,'shot_plan':{'user_locked':{'size':'full'}}})
        self.assertFalse(result['pass']);self.assertTrue(result['shot_constraint_mismatch'])
    async def test_day_and_night_are_different_targets_even_when_labels_match(self):
        self.responses=[verdict(False),verdict(True)];await self.check()
        self.assertTrue((await self.check({**self.snapshot,'local_time':'2026-11-05T12:00:00+08:00'}))['pass']);self.assertEqual(self.calls,2)
    async def test_partial_known_failure_cannot_be_downgraded_to_pixels_only(self):
        value=verdict();value.pop('findings');value['anatomy_ok']=False;self.responses=[value]
        result=await self.check(settings={'qa_unavailable_policy':'pixels_only'})
        self.assertFalse(result['pass']);self.assertTrue(result['schema_incomplete'])
    async def test_malformed_findings_keep_explicit_rejection(self):
        value=verdict(False);value['findings']=[{'evidence':'bad fingers'}];self.responses=[value]
        result=await self.check(settings={'qa_unavailable_policy':'pixels_only'})
        self.assertFalse(result['pass']);self.assertTrue(result['schema_incomplete'])
        self.assertFalse((await self.check(settings={'qa_daily_budget':0,'qa_exhausted_policy':'pixels_only'}))['pass'])
        self.assertEqual(self.calls,1)
    async def test_non_object_qa_is_withheld_instead_of_crashing(self):
        self.responses=[[]]
        result=await self.check();self.assertFalse(result['pass']);self.assertEqual(result['status'],'vision_failed')
    async def test_known_rejection_cannot_be_bypassed_by_qa_budget_degradation(self):
        self.responses=[verdict(False)];await self.check()
        result=await self.check(settings={'qa_daily_budget':0,'qa_exhausted_policy':'pixels_only'})
        self.assertFalse(result['pass']);self.assertEqual(self.calls,1)

class PromptTests(unittest.TestCase):
    def snapshot(self):
        return {'identity':'long purple hair, golden eyes','outfit_tags':'white long sleeves, dark shorts, indoor slippers','setting_tags':'home desk',
                'action':'sitting at the desk, fingertip at the page corner, eyes lowered to the margin', 'props':'open book','emotion':'smiling',
                'style':'life','camera':'detail close up, face outside frame','local_time':bot.now().isoformat(),
                'creative_brief':{'focal_point':'the paused fingertip on the page corner and her eyes on the margin','atmosphere':'quiet room'},
                'shot_plan':{'size':'detail','direction':'front','tags':'overhead view','expectations':{'face_expected':False,'eyes_expected':False}}}
    def test_detail_final_prompt_does_not_reintroduce_hidden_face_or_bottoms(self):
        snapshot=self.snapshot();tags=director.build_tags(snapshot)
        self.assertNotIn('eyes lowered',tags);self.assertNotIn('her eyes',tags);self.assertNotIn('dark shorts',tags);self.assertNotIn('slippers',tags)
        self.assertIn('fingertip',tags);self.assertIn('open book',tags)
        snapshot['style']='comfort';prompt=image_workflow.prompt(snapshot,{},'legacy')
        self.assertNotIn('her eyes',prompt);self.assertNotIn('eyes lowered',prompt);self.assertIn('fingertip',prompt)
    def test_portrait_focus_remains_available_when_face_is_in_frame(self):
        snapshot=self.snapshot();snapshot['shot_plan']['size']='close';snapshot['shot_plan']['expectations']['face_expected']=True
        self.assertIn('her eyes',shot_director.visible_focus(snapshot))
    def test_eyes_focus_does_not_reward_a_rear_close_up(self):
        snapshot=self.snapshot();snapshot['creative_brief']['focal_point']='her face and eyes looking at the book'
        plan=shot_director.choose(snapshot,'',[])
        self.assertTrue(plan['expectations']['face_expected']);self.assertTrue(plan['expectations']['eyes_expected'])
    def test_recent_failed_hands_change_auto_frame_but_not_an_explicit_override(self):
        snapshot=self.snapshot();snapshot['creative_brief']['focal_point']='fingertips at page corner'
        entry=shot_director.history_entry(snapshot,{'pass':False,'anatomy_ok':False},{},'failed')
        plan=shot_director.choose(snapshot,'',[entry]);self.assertNotEqual(plan['size'],'detail');self.assertTrue(plan['avoided_recent_hand_failure'])
        self.assertEqual(shot_director.choose(snapshot,'手部特写',[entry])['size'],'detail')
        entry['at']-=86401;self.assertFalse(shot_director.choose(snapshot,'',[entry]).get('avoided_recent_hand_failure'))

if __name__=='__main__':unittest.main()
