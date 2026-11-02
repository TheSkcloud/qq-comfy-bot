import asyncio,copy,json,tempfile,unittest
from datetime import timedelta
from unittest.mock import AsyncMock,patch
from pathlib import Path
import bot,image_director as director
from image_policy import image_requested,visual_request_context
import test_upgrade

DANCE=[{'role':'assistant','content':'刚练完发声和基础舞步。'},
       {'role':'user','content':'你跳舞的时候是什么样子的？'},
       {'role':'assistant','content':'动作基本到位。'}]
POSTER=[{'role':'user','content':'作为偶像，你也有海报吧'},
        {'role':'assistant','content':'有，但没有摆在房间里。'}]


class IntentTests(unittest.TestCase):
    def test_direct_character_appearance_is_an_image_request(self):
        for text in ('想看看你跳舞什么样子','想看你跳舞的样子','哦？你跳舞的时候是什么样子的🤔',
                     '你弹琴的时候是什么样子','让我看看你今天的穿搭','我想看看海报',
                     '海报是什么风格？我可以看看吗','给我画一张你写代码的样子'):
            with self.subTest(text=text):self.assertTrue(image_requested(text))

    def test_visual_followups_resolve_the_recent_topic(self):
        for history in (DANCE,POSTER):
            for text in ('就那样是什么样？我想看看','我可以看看吗','能让我看一眼吗','我能看看吗'):
                with self.subTest(text=text,history=history):self.assertTrue(image_requested(text,history))

    def test_questions_opinions_refusals_and_technical_views_do_not_draw(self):
        for text in ('你会跳舞吗','你跳舞的时候是什么感觉','你觉得跳舞怎么样','想看看你怎么看这件事',
                     '看看你怎么写这个代码','我想看看运行日志','给我看看这个算法的思路',
                     '不要画图','不想看你跳舞了','海报先不看了','不用给我看海报','你的照片很好看'):
            with self.subTest(text=text):self.assertFalse(image_requested(text,POSTER))

    def test_unrelated_new_topic_or_expired_reference_does_not_reopen_image(self):
        self.assertFalse(image_requested('我可以看看吗'))
        unrelated=POSTER+[{'role':'user','content':'换个话题，说说今天考试'}, {'role':'assistant','content':'考试成绩还没出来。'}]
        self.assertFalse(image_requested('我可以看看吗',unrelated))
        expired=[{**m,'at':(bot.now()-timedelta(hours=1)).isoformat()} for m in POSTER]
        self.assertFalse(image_requested('我可以看看吗',expired))
        self.assertFalse(image_requested('我想看看',[{'role':'user','content':'帮我看代码'},{'role':'assistant','content':'等一下。'}]))

    def test_referenced_poster_purpose_and_current_target_are_retained(self):
        self.assertEqual(visual_request_context('我可以看看吗',POSTER)['purpose'],'poster')
        self.assertEqual(visual_request_context('想看看你跳舞的样子',POSTER)['purpose'],'illustration')

    def test_explicit_context_never_uses_automatic_event_limits(self):
        session={'state':copy.deepcopy(bot.DEFAULT),'history':DANCE,'drawing_attempts':[{'event':'old'}]}
        decision=director.decide(session,'就那样是什么样？我想看看',{},bot.now(),{'daily_budget':0,'initiative':0,'quiet_start':0,'quiet_end':23},False)
        self.assertTrue(decision['allowed']);self.assertEqual(decision['kind'],'explicit')
        self.assertEqual(decision['reason'],'explicit_user_request')


class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data

    async def finish(self):
        while any(not task.done() for task in self.instance.tasks):
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)

    async def check_request(self,text,history):
        session=self.instance.store.get(self.key);session['history']=copy.deepcopy(history)
        self.instance.store.save(self.key,session)
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        self.instance.warm_image_engine=AsyncMock()
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())) as model,patch.object(director,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,text)
            await self.finish()
        self.assertTrue(model.call_args.args[4])
        self.assertTrue(self.instance.context.build.call_args.kwargs['visual_requested'])
        self.instance.warm_image_engine.assert_awaited_once()
        jobs=[json.loads(p.read_text(encoding='utf-8')) for p in (self.root/'data/image_jobs').glob('*/job.json')]
        self.assertEqual(len(jobs),1);job=jobs[0]
        self.assertEqual(job['decision']['kind'],'explicit');self.assertEqual(job['lifecycle'],'sent')
        return job

    async def test_actual_dance_appearance_enters_explicit_pipeline(self):
        await self.check_request('哦？你跳舞的时候是什么样子的🤔',DANCE[:1])

    async def test_actual_dance_followup_enters_explicit_pipeline(self):
        job=await self.check_request('就那样是什么样？我想看看',DANCE)
        self.assertEqual(job['request_detection'],'recent_visual_context')

    async def test_actual_poster_followup_enters_explicit_pipeline(self):
        job=await self.check_request('我可以看看吗',POSTER)
        self.assertEqual(job['snapshot']['purpose'],'poster');self.assertIn('poster layout',job['prompt'])
        self.assertNotIn(job['snapshot']['shot_plan']['size'],('wide','extreme_wide','detail'))

    async def test_visual_repair_receives_the_resolved_poster_reference(self):
        session=self.instance.store.get(self.key);session['history']=copy.deepcopy(POSTER);self.instance.store.save(self.key,session)
        data=self.image_data();data['visual_scene']={}
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch('image_planner.complete_visual',AsyncMock(return_value=self.image_data())) as repair,patch.object(director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'我可以看看吗');await self.finish()
        self.assertEqual(repair.await_count,1);self.assertIn('海报',repair.call_args.args[4])

    async def test_technical_view_stays_chat_even_after_visual_topic(self):
        session=self.instance.store.get(self.key);session['history']=copy.deepcopy(POSTER);self.instance.store.save(self.key,session)
        with patch.object(bot,'deepseek',AsyncMock(return_value=copy.deepcopy(self.data))) as model:
            await self.instance.handle(self.event,self.key,'看看你怎么写代码');await self.finish()
        self.assertFalse(model.call_args.args[4]);self.assertFalse(list((self.root/'data/image_jobs').glob('*/job.json')))


if __name__=='__main__':unittest.main()
