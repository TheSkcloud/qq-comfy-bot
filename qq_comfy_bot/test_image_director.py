import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock,patch
import bot,image_director as d

class ImageDirectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.current=datetime(2026,11,1,21,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.session={'state':{**bot.DEFAULT,'clothing':'白衬衫','location':'房间书桌旁'},'history':[],
                      'scene_hair':'long blue-violet hair','life_state':{'character':'card:suzuki_hana',
                      'activity':'饭后在桌边喝水','started_at':self.current.isoformat(),'expires_at':(self.current+timedelta(minutes=30)).isoformat()}}
        self.data={'state':copy.deepcopy(self.session['state']),'reply':'这杯水还温着。','send_image':True,
                   'visual_tags':'woman','visual_scene':{'identity':'woman','outfit':'white shirt','setting':'indoors, desk',
                   'action':'holding a cup','composition':'medium shot'},
                   'image_plan':{'kind':'life','event':'饭后喝水','evidence':'桌边喝水','relevance':.9}}

    def test_event_policy_cooldown_quiet_and_explicit_priority(self):
        settings={'initiative':1}
        self.assertTrue(d.decide(self.session,'你在干嘛',self.data,self.current,settings)['allowed'])
        self.assertEqual(d.decide(self.session,'你在干嘛',self.data,self.current.replace(hour=23),settings)['reason'],'quiet_hours')
        self.assertTrue(d.decide(self.session,'拍照',self.data,self.current.replace(hour=23),settings,False)['allowed'])
        self.session['drawing_attempts']=[{'at':self.current.isoformat(),'day':'2026-11-01','kind':'explicit'}]
        self.assertEqual(d.decide(self.session,'你在干嘛',self.data,self.current,settings)['reason'],'cooldown')

    def test_missed_gathering_comfort_no_assumed_role_movement(self):
        text='今天上党课，其他人都去吃烧烤了😭'
        self.data['image_plan']={'kind':'comfort','evidence':'党课','relevance':.9}
        self.data.update(image_value={'suitable':True,'kind':'comfort','reason':'针对错过聚会的挫折'},emotion_state={'category':'disappointment','intensity':.7,'evidence':'其他人都去吃烧烤了'})
        self.assertTrue(d.decide(self.session,text,self.data,self.current,{'initiative':1,'comfort':1})['allowed'])
        self.data['state']['location']='烧烤店'
        d.transition(self.session,self.data,text,self.current)
        self.assertEqual(self.data['state']['location'],'房间书桌旁')

    def test_snapshot_is_frozen_and_styles_are_distinct(self):
        shot=d.freeze(self.session,self.data,{'style':'life'},self.current,{})
        self.session['state']['location']='海边'
        self.assertEqual(shot['location'],'房间书桌旁')
        self.assertIn('night',bot.visual_prompt(d.build_tags(shot),21,frozen=True))
        styles=[d.build_tags({**shot,'style':s}) for s in ('life','comfort','comic')]
        self.assertEqual(len(set(styles)),3)

    def test_outfit_requires_transition_and_hair_never_changes_by_date(self):
        self.data['state']['clothing']='黄色连帽衫'
        d.transition(self.session,self.data,'说说今天的事',self.current)
        self.assertEqual(self.data['state']['clothing'],'白衬衫')
        self.data['state']['clothing']='黄色连帽衫'
        d.transition(self.session,self.data,'换上黄色连帽衫',self.current)
        self.assertEqual(self.data['state']['clothing'],'黄色连帽衫')
        self.data['scene_update']={'hairstyle':'blue-violet ponytail'}
        d.transition(self.session,self.data,'把头发扎成马尾',self.current)
        self.assertEqual(self.data['_scene_hair'],'blue-violet ponytail')

    async def test_priority_and_cancelled_waiter_do_not_block_gpu(self):
        slots=d.PrioritySlots();order=[];ready=asyncio.Event();release=asyncio.Event()
        async def work(label,priority,hold=False):
            async with slots.slot(priority):
                order.append(label)
                if hold:ready.set();await release.wait()
        active=asyncio.create_task(work('active',30,True));await ready.wait()
        low=asyncio.create_task(work('life',30));high=asyncio.create_task(work('explicit',0))
        cancel=asyncio.create_task(work('cancel',10));await asyncio.sleep(0);cancel.cancel();await asyncio.gather(cancel,return_exceptions=True)
        release.set();await asyncio.gather(active,low,high)
        self.assertEqual(order,['active','explicit','life'])

    async def test_topic_change_and_continuous_requests_cancel_old_job(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'ROOT',Path(folder)):
            instance=bot.Bot(None);key='private:0:42';event={'message_type':'private','user_id':42}
            async def pending():await asyncio.Event().wait()
            old=asyncio.create_task(pending());await asyncio.sleep(0);instance.image_tasks[key]=old
            instance.drain_inbox=AsyncMock()
            instance.enqueue(event,key,'不说这个了，聊物理')
            await asyncio.sleep(0)
            self.assertFalse(old.done())
            instance.enqueue(event,key,'取消画图')
            await asyncio.wait_for(asyncio.gather(old,return_exceptions=True),1)
            self.assertTrue(old.cancelled())
            later=asyncio.create_task(pending());await asyncio.sleep(0);instance.image_tasks[key]=later
            instance.enqueue(event,key,'再拍一张')
            await asyncio.sleep(0)
            self.assertFalse(later.done())
            instance.enqueue(event,key,'取消画图')
            await asyncio.wait_for(asyncio.gather(later,return_exceptions=True),1)
            self.assertTrue(later.cancelled())
            await asyncio.gather(*instance.tasks);instance.store.db.close()

    async def test_bad_pixels_cost_no_vision_request(self):
        http=type('NoNetwork',(),{'post':lambda *a,**k:(_ for _ in ()).throw(AssertionError('network'))})()
        with tempfile.TemporaryDirectory() as root:
            result=await d.inspect_image(http,b'broken',{},root,{})
        self.assertFalse(result['pass']);self.assertEqual(result['status'],'decode_failed')

    async def test_delivery_guard_inside_shared_lock_blocks_stale_image(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'ROOT',Path(folder)):
            instance=bot.Bot(None);instance.connected.set();instance.call=AsyncMock()
            with self.assertRaises(bot.Superseded):
                await instance.send({'message_type':'private','user_id':42},image=b'png',background=True,image_guard=lambda:False)
            instance.call.assert_not_called();instance.store.db.close()

    async def test_cancel_before_dispatch_finishes_without_waiting_for_connection(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'ROOT',Path(folder)):
            instance=bot.Bot(None);key='private:0:42';instance.store.save(key,self.session)
            instance.comfy.image=AsyncMock(return_value=b'pixels')
            job=d.Job(Path(folder)/'jobs',d.freeze(self.session,self.data,{'style':'life'},self.current,{}),{'priority':0},'prompt',1)
            with patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
                task=asyncio.create_task(instance.render_live({'message_type':'private','user_id':42},key,'prompt',[],self.session['state'],1,job=job))
                instance.image_tasks[key]=task
                for _ in range(10):await asyncio.sleep(0)
                task.cancel();await asyncio.wait_for(task,.5)
            self.assertEqual(job.data['status'],'cancelled');instance.store.db.close()

    async def test_cancel_after_dispatch_keeps_acknowledgement_and_commit(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'ROOT',Path(folder)):
            instance=bot.Bot(None);instance.connected.set();key='private:0:42';instance.store.save(key,self.session)
            instance.comfy.image=AsyncMock(return_value=b'pixels')
            handed=asyncio.Event();ack=asyncio.Event()
            async def call(*a):handed.set();await ack.wait();return {'message_id':123}
            instance.call=call
            job=d.Job(Path(folder)/'jobs',d.freeze(self.session,self.data,{'style':'life'},self.current,{}),{'priority':0},'prompt',1)
            with patch.object(d,'inspect_image',AsyncMock(return_value={'pass':True})):
                task=asyncio.create_task(instance.render_live({'message_type':'private','user_id':42},key,'prompt',[],self.session['state'],1,job=job));instance.image_tasks[key]=task
                await asyncio.wait_for(handed.wait(),1);task.cancel();ack.set();await asyncio.wait_for(task,1)
            self.assertTrue(instance.store.get(key).get('last_image_sent_at'))
            self.assertTrue(any(e.get('qq_result',{}).get('message_id')==123 for e in job.data['events']))
            instance.store.db.close()

if __name__=='__main__':unittest.main()
