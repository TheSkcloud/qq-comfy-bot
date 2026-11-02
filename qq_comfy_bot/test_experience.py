import asyncio,copy,unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch,AsyncMock
import bot,message_style,response_plan,image_candidates as c,social_reasoning,initiative,image_director as d
import test_remediation

class StyleTests(unittest.TestCase):
    def test_short_reply_is_not_split(self):
        self.assertEqual(message_style.split_prose('行。知道了。'),['行。知道了。'])
    def test_long_prose_splits_at_sentence_boundaries_without_losing_content(self):
        text='事情前后的经过得分开看，不能只拿最后的结果来判断整个过程。这里还有另外一个问题，暂时不必急着把所有原因都归到自己身上。你已经说清楚的部分，我也没有必要换种说法再解释一遍。先留在这个判断就行。'
        result=message_style.split_prose(text)
        self.assertTrue(2<=len(result)<=4);self.assertEqual(''.join(result),text)
        self.assertTrue(all(s.endswith(('。','！','？')) for s in result))
    def test_code_and_quoted_phrase_stay_intact(self):
        code='解释：\n```python\n'+('print(1)\n'*20)+'```'
        self.assertEqual(message_style.split_prose(code),[code])
        text='他刚才说：“这个项目其实很复杂。不能只看最后的结果。”然后又把整个流程解释了一遍。'+('这件事还是需要结合前后的情况判断。'*4)
        result=message_style.split_prose(text)
        self.assertTrue(any('“这个项目其实很复杂。不能只看最后的结果。”' in s for s in result))
    def test_four_segments_length_and_attitude_are_preserved(self):
        plan=response_plan.parse({'response_plan':{'segments':['一','二','三','四'],'length':'normal','attitude':'disagreement','attitude_reason':'对当前观点有不同看法'}})
        self.assertEqual(len(plan['segments']),4);self.assertEqual(plan['attitude'],'disagreement')
    def test_away_for_days_can_share_but_never_contact_is_blocked(self):
        now=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'));cfg={'proactive_enabled':True,'drawing':{'life_contact_window_days':14}}
        self.assertTrue(initiative.eligible({'last_user_at':(now-timedelta(days=3)).isoformat()},now,cfg))
        self.assertFalse(initiative.eligible({},now,cfg))
    def test_mimicry_opportunity_does_not_require_model_to_prefer_images(self):
        now=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'));session={'state':copy.deepcopy(bot.DEFAULT),'history':[]}
        candidate=c.conversation('k',session,'我刚刚是在模仿你说话',{},now)
        self.assertEqual(candidate.source_type,'comic')
        self.assertIsNone(c.conversation('k',session,'这家连锁店好吃吗',{'response_plan':{'intent':'teasing'}},now))
    def test_explicit_image_veto_and_light_fatigue_remain_respected(self):
        now=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'));session={'state':copy.deepcopy(bot.DEFAULT),'history':[]}
        self.assertIsNone(c.conversation('k',session,'我刚刚是在模仿你',{'image_value':{'veto':True}},now))
        self.assertIsNone(c.conversation('k',session,'今天有点累',{},now))
    def test_repeated_disappointment_can_motivate_comfort(self):
        now=datetime(2026,11,1,15,tzinfo=ZoneInfo('Asia/Shanghai'));session={'state':copy.deepcopy(bot.DEFAULT),'history':[],'social_state':[{'category':'disappointment'},{'category':'disappointment'}]}
        result=c.conversation('k',session,'这次还是没选上',{},now)
        self.assertEqual(result.source_type,'comfort')
    def test_important_event_must_not_be_reduced_to_bare_acknowledgement(self):
        self.assertTrue(message_style.empty_important_reaction({'reply':'嗯。'},'我很受打击，我很心痛',[]))
        self.assertFalse(message_style.empty_important_reaction({'reply':'嗯。'},'好',[]))
        self.assertFalse(message_style.empty_important_reaction({'reply':'这落差确实不小。'},'我很受打击',[]))
    def test_normal_parallel_chat_is_not_cancelled_by_word_other(self):
        self.assertEqual(d.image_instruction('我先去看看别的，画着就行。'),'keep')
        for text in ('别画了','不用给我画图','不要再画了','取消画图'):
            self.assertEqual(d.image_instruction(text),'cancel')
    def test_chinese_action_uses_english_five_part_action(self):
        now=datetime(2026,11,1,21,tzinfo=ZoneInfo('Asia/Shanghai'))
        data={'visual_scene':{'action':'sitting at a desk, reading an open book','composition':'side view'},'image_plan':{'action':'坐在书桌前读书','camera':'侧面'}}
        shot=d.freeze({'state':copy.deepcopy(bot.DEFAULT)},data,{'style':'life'},now,{})
        self.assertIn('reading',shot['action']);self.assertEqual(shot['camera'],'side view')
    def test_legacy_complex_help_gets_essential_plan_without_extra_model_call(self):
        data=response_plan.normalize({'reply':'先列出条件，再计算结果。'},copy.deepcopy(bot.DEFAULT),'用动量守恒分析碰撞，列出所有适用条件和一个计算例子。')
        self.assertEqual(data['response_plan']['length'],'extended')
        self.assertTrue(all(p=='essential' for p in data['response_plan']['segment_priority']))
    def test_complete_reply_survives_broken_optional_json_without_state_or_image(self):
        result=response_plan.decode('{"reply":"行啊，确实不错。","state":{"location":"海边"}, {"type":"json_object"}')
        self.assertEqual(result['reply'],'行啊，确实不错。')
        self.assertEqual(result['state'],{});self.assertFalse(result['send_image'])
        self.assertTrue(result['_text_only_recovery'])
    def test_explicit_image_never_uses_partial_json(self):
        with self.assertRaises(ValueError):response_plan.decode('{"reply":"行。", broken',True)
    def test_budgets_can_exceed_old_four_per_day(self):
        cfg=d.config({'daily_budget':12,'qa_daily_budget':24,'life_candidates_per_day':12,'kind_limits':{'life':{'daily_budget':8}}})
        self.assertEqual(cfg['daily_budget'],12);self.assertEqual(cfg['kind_limits']['life']['daily_budget'],8)

class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_remediation.PipelineTests.asyncSetUp
    asyncTearDown=test_remediation.PipelineTests.asyncTearDown
    async def test_interruption_preserves_sent_receipt_and_carries_only_essential_unsent(self):
        wait=asyncio.Event();release=asyncio.Event()
        old=copy.deepcopy(self.data);old['response_plan']={'intent':'advice','length':'extended','segments':['第一条已确认','后续关键步骤','可省略的总结'],'segment_priority':['essential','essential','optional']}
        async def model(*args,**kwargs):return copy.deepcopy(old if args[3]=='原问题' else self.data)
        def delay(ticket,length,segment=0):
            if ticket.version==1 and segment:wait.set();return 60
            return 0
        self.instance.scheduler.remaining=delay
        with patch.object(bot,'deepseek',model):
            self.instance.enqueue(self.event,self.key,'原问题')
            await asyncio.wait_for(wait.wait(),3)
            self.instance.enqueue(self.event,self.key,'换个问题')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        history=self.instance.store.get(self.key)['history']
        self.assertEqual(sum(m['content']=='第一条已确认' for m in history),1)
        self.assertFalse(any(m['content']=='后续关键步骤' for m in history))
        self.assertEqual(self.instance.store.get(self.key)['pending_essential']['segments'],['后续关键步骤'])
    async def test_each_segment_records_its_own_ack(self):
        data=copy.deepcopy(self.data);data['response_plan']={'segments':['第一句','第二句']}
        with patch.object(bot,'deepseek',AsyncMock(return_value=data)):
            self.instance.enqueue(self.event,self.key,'说说你的想法')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)),3)
        plan=self.instance.store.get(self.key)['delivery_plan']
        self.assertEqual([s['status'] for s in plan['segments']],['sent','sent'])
        self.assertEqual([s['qq_result']['message_id'] for s in plan['segments']],[1,2])

if __name__=='__main__':unittest.main()
