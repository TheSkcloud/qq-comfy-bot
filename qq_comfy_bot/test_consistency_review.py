import asyncio,copy,json,sqlite3,tempfile,unittest,itertools
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock,patch
import bot,delivery_state,dialogue_depth,image_candidates,image_director,image_mode
from session_store import SessionConflict
import test_upgrade

class StoreTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'sessions.db';self.store=bot.Store(self.path)
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_legacy_two_column_database_is_migrated_without_changing_values(self):
        self.store.db.close();self.path.unlink()
        value={'state':copy.deepcopy(bot.DEFAULT),'history':[{'role':'user','content':'原有记录'}]}
        db=sqlite3.connect(self.path)
        try:
            with db:db.execute('CREATE TABLE sessions(key TEXT PRIMARY KEY,value TEXT)');db.execute('INSERT INTO sessions VALUES (?,?)',('k',json.dumps(value)))
        finally:db.close()
        self.store=bot.Store(self.path)
        self.assertEqual(dict(self.store.get('k')),value)
        self.assertEqual(self.store.get('k').revision,0)
    def test_stale_reply_preserves_background_candidate_and_last_job(self):
        main=self.store.get('k');background=self.store.get('k')
        background.update(candidate_attempts=['candidate'],last_image_job_id='a'*32);self.store.save('k',background)
        main['history']=[{'role':'assistant','content':'已确认的正文'}];self.store.save('k',main)
        actual=self.store.get('k');self.assertEqual(actual['candidate_attempts'],['candidate']);self.assertEqual(actual['last_image_job_id'],'a'*32)
        self.assertEqual(actual.revision,2)
    def test_independent_connections_merge_nested_fields_and_appends(self):
        self.store.save('k',{'state':copy.deepcopy(bot.DEFAULT),'history':[],'meta':{'a':0,'b':0}})
        other=bot.Store(self.path)
        try:
            left=self.store.get('k');right=other.get('k')
            left['meta']['a']=1;left['history'].append({'id':'left','role':'assistant','content':'左'});self.store.save('k',left)
            right['meta']['b']=2;right['history'].append({'id':'right','role':'assistant','content':'右'});other.save('k',right)
            actual=self.store.get('k');self.assertEqual(actual['meta'],{'a':1,'b':2});self.assertEqual([m['id'] for m in actual['history']],['left','right'])
            other.save('k',right);self.assertEqual(len(self.store.get('k')['history']),2)
        finally:other.db.close()
    def test_conflicting_scene_update_is_rejected_instead_of_overwritten(self):
        left=self.store.get('k');right=self.store.get('k')
        left['state']['location']='厨房';self.store.save('k',left)
        right['state']['location']='海边'
        with self.assertRaises(SessionConflict):self.store.save('k',right)
        self.assertEqual(self.store.get('k')['state']['location'],'厨房')
    def test_truncated_history_keeps_both_concurrent_acknowledgements(self):
        value=self.store.get('k');value['history']=[{'id':str(i),'content':str(i)} for i in range(20)];self.store.save('k',value)
        a=self.store.get('k');b=self.store.get('k')
        for value,name in ((a,'a'),(b,'b')):
            value['history']=(value['history']+[{'id':name,'content':name}])[-20:];self.store.save('k',value)
        history=self.store.get('k')['history'];self.assertEqual(len(history),20);self.assertEqual([m['id'] for m in history[-2:]],['a','b'])
    def test_all_writer_orders_keep_candidate_receipt_and_image_fields(self):
        for order in itertools.permutations(range(3)):
            key=str(order);items=[self.store.get(key) for _ in range(3)]
            items[0]['candidate_attempts']=['candidate'];items[0]['history'].append({'id':'c','content':'候选'})
            items[1]['delivery_receipts']=[{'id':'receipt'}];items[1]['history'].append({'id':'t','content':'文字'})
            items[2]['last_image_job_id']='f'*32;items[2]['history'].append({'id':'i','content':'图片'})
            for index in order:self.store.save(key,items[index])
            value=self.store.get(key)
            self.assertEqual({m['id'] for m in value['history']},{'c','t','i'})
            self.assertEqual(value['candidate_attempts'],['candidate']);self.assertEqual(value['delivery_receipts'],[{'id':'receipt'}]);self.assertEqual(value['last_image_job_id'],'f'*32)

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data
    async def workers(self):
        await asyncio.wait_for(asyncio.gather(*list(self.instance.inbox_workers.values())),3)
    def narrated(self):
        data=copy.deepcopy(self.data);data['state'].update(location='家中厨房')
        data.update(reply='先接着聊。\n我回到厨房准备晚饭。',scene_update={'field':'location','evidence':'我回到厨房准备晚饭。'},
            life_update={'activity':'在厨房准备晚饭','minutes':30,'evidence':'我回到厨房准备晚饭。'},affect={'joy':2})
        data['response_plan']={'intent':'share','segments':['先接着聊。','我回到厨房准备晚饭。']}
        return data
    async def test_cancel_before_first_ack_leaves_no_narrated_state_or_activity(self):
        waiting=asyncio.Event();data=self.narrated()
        self.instance.scheduler.remaining=lambda *args:(waiting.set() or 60)
        mood_calls=[];self.instance.context.life.mood=lambda *a,**kw:mood_calls.append(a) or '平静'
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            self.instance.enqueue(self.event,self.key,'继续说刚才的事')
            await asyncio.wait_for(waiting.wait(),2)
            self.instance.enqueue(self.event,self.key,'/取消');self.instance.scheduler.remaining=lambda *args:0
            await self.workers()
        actual=self.instance.store.get(self.key)
        self.assertEqual(actual['state'],bot.DEFAULT);self.assertNotIn('life_state',actual)
        self.assertFalse(any('我回到厨房' in m['content'] for m in actual['history'] if m['role']=='assistant'))
        self.assertFalse(any(call[1].get('joy') for call in mood_calls if len(call)>1 and isinstance(call[1],dict)))
    async def test_first_ack_does_not_commit_an_unsent_second_segment_claim(self):
        waiting=asyncio.Event();data=self.narrated()
        def pacing(ticket,length,segment=0):
            if ticket.version==1 and segment:waiting.set();return 60
            return 0
        self.instance.scheduler.remaining=pacing
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            self.instance.enqueue(self.event,self.key,'继续说刚才的事');await asyncio.wait_for(waiting.wait(),2)
            actual=self.instance.store.get(self.key);self.assertEqual(actual['state'],bot.DEFAULT);self.assertNotIn('life_state',actual)
            self.instance.enqueue(self.event,self.key,'/取消');await self.workers()
        self.assertEqual(self.instance.store.get(self.key)['state'],bot.DEFAULT)
        self.assertEqual([m['content'] for m in self.instance.store.get(self.key)['history'] if m['role']=='assistant'],['先接着聊。'])
    async def test_matching_ack_commits_scene_and_evidenced_life_once(self):
        data=self.narrated()
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            await self.instance.handle(self.event,self.key,'继续说刚才的事')
        actual=self.instance.store.get(self.key)
        self.assertEqual(actual['state']['location'],'家中厨房');self.assertEqual(actual['life_state']['phase'],'text_acknowledged')
        self.assertEqual(actual['scene_version'],1);self.assertEqual(actual['pending_turn']['status'],'acknowledged')
    async def test_ack_interleaving_keeps_candidate_history_and_job_fields(self):
        original=self.instance.call
        async def call(*args):
            live=self.instance.store.get(self.key);live['candidate_attempts']=['kept'];live['last_image_job_id']='a'*32
            live['history'].append({'role':'assistant','content':'[后台图片已实际发送成功]','at':bot.now().isoformat()});self.instance.store.save(self.key,live)
            return await original(*args)
        self.instance.call=call
        with patch.object(bot,'deepseek',AsyncMock(return_value=copy.deepcopy(self.data))):await self.instance.handle(self.event,self.key,'聊聊海报')
        actual=self.instance.store.get(self.key)
        self.assertEqual(actual['candidate_attempts'],['kept']);self.assertEqual(actual['last_image_job_id'],'a'*32)
        self.assertTrue(any('后台图片' in m['content'] for m in actual['history']));self.assertTrue(any(m['content']=='最新回复' for m in actual['history']))
        self.assertEqual(actual['delivery_plan']['segments'][0]['status'],'sent')
    async def test_unspoken_life_metadata_is_not_treated_as_current_activity(self):
        data=copy.deepcopy(self.data);data['life_update']={'activity':'在海边跑步','minutes':30}
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):await self.instance.handle(self.event,self.key,'随便说说')
        self.assertNotIn('life_state',self.instance.store.get(self.key))
    async def test_text_ack_without_message_id_cannot_confirm_character_claims(self):
        data=self.narrated();attempts=[]
        async def call(action,params,ws):attempts.append(params);return {}
        self.instance.call=call
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            await self.instance.handle(self.event,self.key,'继续说刚才的事')
        actual=self.instance.store.get(self.key)
        self.assertEqual(actual['state'],bot.DEFAULT);self.assertNotIn('life_state',actual)
        self.assertFalse(any(m['role']=='assistant' for m in actual['history']))
    async def test_explicit_silent_image_changes_scene_only_after_image_ack(self):
        data=self.image_data();data['state']['clothing']='黄色连帽衫';data['visual_scene']['outfit']='yellow hoodie'
        data['response_plan']={'mode':'silent','should_reply':False,'segments':[]}
        started=asyncio.Event();release=asyncio.Event()
        async def draw(*args,**kwargs):started.set();await release.wait();return b'image'
        self.instance.comfy.image=draw
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)),patch.object(image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            await self.instance.handle(self.event,self.key,'换上黄色连帽衫，画一张')
            await asyncio.wait_for(started.wait(),2);self.assertEqual(self.instance.store.get(self.key)['state'],bot.DEFAULT)
            release.set();await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        self.assertEqual(self.instance.store.get(self.key)['state']['clothing'],'黄色连帽衫')
    async def test_cancelled_input_rebuilt_with_supplement_is_not_received_twice(self):
        started=asyncio.Event();calls=[]
        async def model(http,state,history,text,*args,**kwargs):
            calls.append((copy.deepcopy(history),text))
            if len(calls)==1:started.set();await asyncio.Event().wait()
            return copy.deepcopy(self.data)
        with patch.object(bot,'deepseek',model):
            self.instance.enqueue(self.event,self.key,'我今天去了图书馆');await asyncio.wait_for(started.wait(),2)
            self.instance.enqueue(self.event,self.key,'不对，是咖啡馆');await self.workers()
        users=[item['content'] for item in self.instance.store.get(self.key)['history'] if item['role']=='user']
        self.assertEqual(users,['我今天去了图书馆','不对，是咖啡馆'])
        self.assertIn('图书馆\n不对',calls[-1][1]);self.assertFalse(any('图书馆' in item['content'] for item in calls[-1][0]))

class AdmissionThinkingTests(unittest.TestCase):
    def setUp(self):self.current=bot.now().replace(hour=15);self.session={'state':copy.deepcopy(bot.DEFAULT),'history':[]}
    def candidate(self,event='study',activity='在家学习'):
        return image_candidates.make('life','private:0:42',event,'current_fictional_activity',activity,self.session,self.current).data()
    def test_role_learning_is_allowed_but_busy_user_is_respected(self):
        data={'_candidate':self.candidate()}
        self.assertTrue(image_director.decide(self.session,'在家学习',data,self.current,{'initiative':1})['allowed'])
        self.session['history']=[{'role':'user','content':'我现在在学习，晚点聊'}]
        self.assertEqual(image_director.decide(self.session,'在家学习',data,self.current,{'initiative':1})['reason'],'busy_or_closing')
    def test_candidate_probability_zero_one_and_stable_sampling(self):
        for rate,expected in ((0,0),(1,400),(.8,None)):
            values=[image_director.decide(self.session,'在家学习',{'_candidate':self.candidate(str(i))},self.current,{'initiative':rate})['allowed'] for i in range(400)]
            if expected is not None:self.assertEqual(sum(values),expected)
            else:self.assertTrue(280<sum(values)<360)
        data={'_candidate':self.candidate('stable')}
        a=image_director.decide(self.session,'',data,self.current,{'initiative':.5})
        self.assertEqual(a['allowed'],image_director.decide(self.session,'',data,self.current,{'initiative':.5})['allowed'])
    def test_effective_mode_never_reports_preferences_as_enabled_when_global_off(self):
        cfg={'spontaneous_images':True,'proactive_enabled':True}
        self.assertFalse(image_mode.effective(cfg,'off')['proactive_enabled'])
        self.assertTrue(image_mode.effective(cfg,'auto')['proactive_enabled'])
        self.assertFalse(image_mode.effective({**cfg,'spontaneous_images':False},'auto')['proactive_enabled'])
    def test_short_causal_followup_and_unrelated_fifth_turn(self):
        s={}
        for _ in range(4):
            user='这个方案的成本与收益要一起看。';d=dialogue_depth.plan(s,user,[],self.current);dialogue_depth.finish(s,user,'我更倾向留白。',{},d,self.current)
        self.assertEqual(dialogue_depth.plan(s,'凭什么？',[{'role':'user','content':'这次评选没有选我'}],self.current)['stage'],'C')
        self.assertEqual(dialogue_depth.plan(s,'今天早餐吃了一碗粥。',[],self.current)['stage'],'A')
        self.assertEqual(dialogue_depth.plan(s,'那这个方案的维护成本呢？',[],self.current)['stage'],'B')
    def test_all_persisted_image_states_have_a_shared_fact(self):
        from image_states import ACTIVE,TERMINAL,MESSAGES,lifecycle
        for status in ACTIVE|TERMINAL:
            self.assertIn(status,MESSAGES,status);self.assertNotEqual(lifecycle(status),'unknown',status)
        self.assertTrue(ACTIVE.isdisjoint(TERMINAL))
    def test_incidental_hand_under_chin_does_not_hide_an_expression_story(self):
        import shot_director
        snapshot={'style':'comic','action':'sitting at desk, chin on one hand, sideways glance with a smirk','props':'notebook',
            'setting_tags':'room desk','camera':'medium shot','creative_brief':{'focal_point':'chin on one hand, sideways glance with a smirk'}}
        self.assertIn(shot_director.choose(snapshot)['size'],('close','extreme_close'))
        self.assertEqual(shot_director.choose(snapshot,'手部特写')['size'],'detail')

if __name__=='__main__':unittest.main()
