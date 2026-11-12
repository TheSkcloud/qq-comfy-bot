import asyncio,copy,hashlib,json,tempfile,time,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import AsyncMock,patch
from zoneinfo import ZoneInfo
import bot,image_director,image_progress
from image_policy import image_requested,visual_request_context
from reply_scheduler import ReplyScheduler
import test_upgrade


class EllipticalIntentTests(unittest.TestCase):
    def test_named_role_appearance_without_request_verb(self):
        for text in ('铃木学习的样子……','你学习的样子……','羽那读书时的样子','铃木羽那做笔记的样子？','Suzuki Hana跳舞的样子'):
            with self.subTest(text=text):self.assertTrue(image_requested(text))
        self.assertIn('学习',visual_request_context('铃木学习的样子……')['reference'])
    def test_evaluations_user_study_and_technical_views_stay_chat(self):
        for text in ('铃木学习的样子很认真','铃木学习怎么样','我学习的样子','铃木学习时是什么感觉','看看你学习这个算法的思路','铃木学习的样子不用画了','不要画铃木学习的样子'):
            with self.subTest(text=text):self.assertFalse(image_requested(text))
        self.assertFalse(image_requested('我可以看看吗',[{'role':'user','content':'今天的学习安排需要调整'}]))
    def test_study_appearance_is_explicit_even_when_sharing_is_off_and_user_busy(self):
        result=image_director.decide({'state':copy.deepcopy(bot.DEFAULT),'history':[]},'铃木学习的样子……',{},bot.now(),{'daily_budget':0},False)
        self.assertTrue(result['allowed']);self.assertEqual(result['kind'],'explicit')


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=datetime.now(ZoneInfo('Asia/Shanghai'));self.key='private:0:42';self.id='a'*32
        self.s={'history':[],'last_image_job_id':self.id}
    def job(self,status,**fields):
        path=self.root/'data/image_jobs'/self.id;path.mkdir(parents=True,exist_ok=True)
        data={'status':status,'created_at':self.now.timestamp(),'conversation_id':hashlib.sha256(self.key.encode()).hexdigest()[:12],**fields}
        (path/'job.json').write_text(json.dumps(data),encoding='utf-8');return data
    def status(self,**kwargs):return image_progress.snapshot(self.root,self.s,self.key,self.now,**kwargs)
    def test_progress_maps_authoritative_job_states(self):
        expected={'planning':'准备','queued':'排队','submitted':'正在画','ready_to_send':'发送','completed':'发出','failed':'失败','withheld_quality':'没有通过','cancelled':'取消','expired':'停止','rejected':'没有进入'}
        for status,word in expected.items():
            self.job(status);self.assertIn(word,self.status()['text'])
    def test_no_job_or_broken_record_does_not_claim_progress(self):
        self.s.pop('last_image_job_id');self.assertEqual(self.status()['status'],'not_started')
        self.s['last_image_job_id']=self.id;self.assertEqual(self.status()['status'],'unknown')
        self.job([]);self.assertEqual(self.status()['status'],'unknown')
    def test_cross_conversation_and_traversal_are_not_read(self):
        self.job('submitted',conversation_id='another');self.assertEqual(self.status()['status'],'not_started')
        self.s['last_image_job_id']='../../private';self.assertEqual(self.status()['status'],'not_started')
    def test_newer_request_is_not_satisfied_by_an_old_success(self):
        self.job('completed');self.s['history']=[{'role':'user','content':'铃木学习的样子……','at':(self.now+timedelta(minutes=2)).isoformat()}]
        self.assertEqual(self.status()['status'],'not_started')
    def test_pre_restart_active_job_is_not_claimed_to_be_running(self):
        self.job('submitted');self.assertEqual(self.status(started_at=self.now.timestamp()+5)['status'],'interrupted')
    def test_unknown_delivery_must_not_be_retried_as_a_new_request(self):
        self.job('qq_result_unknown');result=self.status();self.assertEqual(result['status'],'outcome_unknown');self.assertIn('不重复',result['text'])
    def test_progress_query_requires_visual_context_for_generic_words(self):
        visual={'history':[{'role':'user','content':'铃木学习的样子……','at':self.now.isoformat()},{'role':'assistant','content':'给你一眼。','at':self.now.isoformat()}]}
        for text in ('好了没🥹','好了吗','还要多久'):
            self.assertTrue(image_progress.is_query(text,visual,self.now));self.assertFalse(image_progress.is_query(text,{'history':[]},self.now))
        self.assertTrue(image_progress.is_query('图片好了没',{'history':[]},self.now))
        self.assertFalse(image_progress.is_query('饭好了没',visual,self.now))
        self.assertFalse(image_progress.is_query('好了没',visual,self.now+timedelta(hours=1)))
    def test_blocked_image_promise_is_replaced_but_other_messages_stay(self):
        plan={'intent':'image_request','mode':'text','segments':['就是坐着看。','……行吧，给你一眼。']}
        result=image_progress.guard_blocked_reply(plan,{'send_image':True})
        self.assertEqual(result['segments'],['这次没有开始出图。'])
        self.assertEqual(image_progress.guard_blocked_reply(plan,{'_drawing_started':True}),plan)
        ordinary={'intent':'casual_chat','segments':['我把代码发给你。']}
        self.assertEqual(image_progress.guard_blocked_reply(ordinary,{'send_image':False}),ordinary)


class EntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data
    async def finish(self):
        while any(not t.done() for t in self.instance.tasks):await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
    def texts(self):return [p[1]['message'][0]['data'].get('text','') for p in self.packets]
    async def test_actual_elliptical_request_creates_one_explicit_owned_job(self):
        self.instance.comfy.image=AsyncMock(return_value=b'pixels')
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())) as model,patch.object(image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'铃木学习的样子……');await self.finish()
        self.assertTrue(model.call_args.args[4]);s=self.instance.store.get(self.key)
        self.assertTrue(s['last_image_job_id']);paths=list((self.root/'data/image_jobs').glob('*/job.json'));self.assertEqual(len(paths),1)
        job=json.loads(paths[0].read_text(encoding='utf-8'));self.assertEqual(job['decision']['kind'],'explicit')
        self.assertEqual(job['conversation_id'],hashlib.sha256(self.key.encode()).hexdigest()[:12])
    async def test_ghost_promise_followup_does_not_call_model_or_gpu(self):
        s=self.instance.store.get(self.key);s['history']=[{'role':'user','content':'铃木学习的样子……','at':bot.now().isoformat()},{'role':'assistant','content':'给你一眼。','at':bot.now().isoformat()}];self.instance.store.save(self.key,s)
        with patch.object(bot,'deepseek',AsyncMock()) as model,patch.object(self.instance.comfy,'image',AsyncMock()) as gpu:
            await self.instance.handle(self.event,self.key,'好了没🥹')
            await self.instance.handle(self.event,self.key,'？')
        model.assert_not_called();gpu.assert_not_called();self.assertEqual(len(self.texts()),2)
        self.assertTrue(all('没有开始出图' in text for text in self.texts()))
    async def test_real_running_job_query_does_not_create_duplicate(self):
        s=self.instance.store.get(self.key);job=image_director.Job(self.root/'data/image_jobs',{}, {'image_task_id':'b'*32},'',0)
        job.data['conversation_id']=hashlib.sha256(self.key.encode()).hexdigest()[:12];job.record('submitted')
        s['last_image_job_id']=job.id;self.instance.store.save(self.key,s)
        with patch.object(bot,'deepseek',AsyncMock()) as model,patch.object(self.instance.comfy,'image',AsyncMock()) as gpu:
            await self.instance.handle(self.event,self.key,'图片好了没')
        model.assert_not_called();gpu.assert_not_called();self.assertIn('正在画',self.texts()[0]);self.assertEqual(len(list((self.root/'data/image_jobs').glob('*/job.json'))),1)
    async def test_blocked_auto_proposal_does_not_send_image_promise(self):
        data=self.image_data();data.update(reply='给你一眼。',response_plan={'intent':'image_request','mode':'text','segments':['给你一眼。']})
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            await self.instance.handle(self.event,self.key,'正在学习使用gpt')
        self.assertEqual(self.texts(),['这次没有开始出图。'])
    async def test_status_command_uses_fast_pace(self):
        remaining=ReplyScheduler().remaining;waits=[]
        def measure(ticket,*args):
            waits.append((ticket.pace,remaining(ticket,*args)));return 0
        self.instance.scheduler.remaining=measure
        await self.instance.handle(self.event,self.key,'/状态')
        self.assertTrue(waits);self.assertTrue(all(pace=='fast' and delay<2 for pace,delay in waits))
    async def test_status_ack_keeps_concurrently_completed_image_history(self):
        original=self.instance.send
        async def concurrent_finish(*args,**kwargs):
            s=self.instance.store.get(self.key);s['drawing_sends']=[{'kind':'explicit','at':bot.now().isoformat()}]
            s['history']=[{'role':'assistant','content':'[图片已实际发送成功]','at':bot.now().isoformat()}]
            self.instance.store.save(self.key,s)
            return await original(*args,**kwargs)
        self.instance.send=concurrent_finish
        await self.instance.handle(self.event,self.key,'图片好了没')
        live=self.instance.store.get(self.key);self.assertEqual(len(live['drawing_sends']),1)
        self.assertIn('图片已实际发送成功',live['history'][0]['content'])

if __name__=='__main__':unittest.main()
