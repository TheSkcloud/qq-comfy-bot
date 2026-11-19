import copy
import json
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch, AsyncMock

import bot
import companion
import dialogue_planner
import initiative
import memory_recall
import social_reasoning
import test_remediation
from life import Life
from workflow_validation import output_images


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.life = Life(self.temp.name)
        self.now = bot.now()

    def record(self, owner, text, kind='preference', days=0):
        return self.life.record(owner, [{'kind':kind,'value':text,'evidence':text}], text,
                                self.now-timedelta(days=days),input_id='evidence-1')

    def test_semantic_paraphrase_and_owner_isolation(self):
        if memory_recall.encoder() is None:
            self.skipTest('Optional local CPU encoder unavailable')
        other = memory_recall.owner('42', '另一个角色')
        text = '我喜欢 King Crimson 的《Red》，尤其喜欢紧张复杂的编曲'
        accepted = self.life.record('42',[{'kind':'preference','value':'King Crimson 的《Red》','evidence':text}],
                                    text,self.now-timedelta(days=23))
        self.assertEqual(len(accepted),1)
        self.record('42','我喜欢骑自行车')
        self.record(other,'我喜欢那张专辑的封面')
        reply = self.life.recall('42','最近又在听红那张专辑',self.now)
        self.assertIn('King Crimson', reply)
        self.assertNotIn('封面', reply)
        self.assertNotIn('自行车', reply)
        self.assertNotIn('King Crimson', self.life.recall(other,'最近又在听红那张专辑',self.now))
        self.assertFalse(self.life.recall('其他用户','红那张专辑',self.now))

    def test_temporal_filter_and_correction_remove_superseded_values(self):
        old, new = '我的耳机是型号甲', '我的耳机换成型号乙'
        self.record('u',old,'basic_fact',days=1)
        self.record('u',new,'basic_fact')
        self.assertEqual([f['value'] for f in self.life.facts('u')],[new])
        self.record('u','我喜欢冰咖啡',days=1)
        found=self.life.recall('u','昨天我提过的咖啡',self.now)
        self.assertIn('冰咖啡',found)
        self.assertNotIn('型号乙',found)
        self.assertTrue(self.life.correct('u',new,'我的耳机是型号丙',self.now))
        self.assertTrue(self.life.forget('u','我的耳机是型号丙'))
        with self.life.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM fact_vectors WHERE slot='basic_fact:current:耳机'").fetchone())
            self.assertEqual(db.execute('SELECT input_id FROM events WHERE value=?',(old,)).fetchone()[0],'evidence-1')

    def test_legacy_database_lexical_fallback_and_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder)/'life.db')) as db, db:
                db.execute('CREATE TABLE facts(user TEXT,kind TEXT,value TEXT,updated TEXT,PRIMARY KEY(user,kind))')
                db.execute('INSERT INTO facts VALUES(?,?,?,?)',('u','preference','我喜欢黑胶唱片',self.now.isoformat()))
            with patch.object(memory_recall,'encoder',return_value=None):
                life=Life(folder)
                result=life.recall('u','上次那个呢',self.now,history=[{'role':'user','content':'聊聊黑胶唱片'}])
                self.assertIn('黑胶唱片',result)
                self.assertFalse(life.recall('u','明天数学考试',self.now))

    def test_capture_records_only_explicit_first_person(self):
        for text in ('他说“我喜欢咖啡”','我喜欢红茶但别记','我喜欢红茶'):
            self.life.capture_before_trim('u',text,self.now,'message')
        self.assertEqual([f['value'] for f in self.life.facts('u')],['我喜欢红茶'])

    def test_past_location_is_scoped_to_day_and_user(self):
        self.record('u','我在图书馆','location',days=1)
        self.record('other','我在学校','location',days=1)
        self.record('u','我在咖啡馆','location')
        result = self.life.recall('u','我昨天在哪里？',self.now)
        self.assertIn('图书馆',result)
        self.assertNotIn('咖啡馆',result)
        self.assertNotIn('学校',result)
        self.assertFalse(self.life.recall('u','我前天在哪里？',self.now))

    def test_opt_out_applies_to_model_extraction(self):
        evidence = '我喜欢冷饮'
        updates = [dict(kind='preference', value=evidence, evidence=evidence)]
        self.assertFalse(self.life.record('u',updates,evidence+'，不要把这件事记下来。',self.now))
        self.assertFalse(self.life.facts('u'))


class DirectionTests(unittest.TestCase):
    def test_story_does_not_establish_an_ulterior_motive(self):
        text = '我转述他的话，他说明天要走。'
        reply = '听到了。你没听出来他是在打什么主意吗？'
        self.assertEqual(social_reasoning.safe_grounded_reply(reply,text,[]),'听到了。')
        self.assertFalse(social_reasoning.grounding_issues(reply,text+'我怀疑他另有目的。',[]))

    def test_quote_topic_switch_and_actual_question(self):
        history=[{'role':'user','content':'上次提到大学生活'}]
        self.assertEqual(dialogue_planner.plan('老师问“为什么？”',history).primary_act,'ListenToStory')
        self.assertEqual(dialogue_planner.plan('那天你听说过这个游戏吗？',history).primary_act,'Answer')
        self.assertFalse(dialogue_planner.plan('换个话题，最近在听唱片',history).topic_continuation)
        self.assertEqual(dialogue_planner.plan('请解释并举例',history).secondary_act,'Example')

    def test_backoff_uses_only_confirmed_proactive_delivery(self):
        now=bot.now()
        session={'last_user_at':(now-timedelta(hours=5)).isoformat(),
                 'drawing_sends':[{'initiative':True,'activity':'读书','at':(now-timedelta(minutes=n)).isoformat()} for n in (90,30)]}
        self.assertEqual(initiative.unanswered(session),2)
        self.assertFalse(initiative.worthwhile(session,'读书'))
        self.assertTrue(initiative.worthwhile(session,'登台演出'))
        session['last_user_at']=now.isoformat()
        self.assertEqual(initiative.unanswered(session),0)

    def test_fixed_character_boundary_and_relation_hint(self):
        state={'state':dict(bot.DEFAULT)}
        companion.confirmed_style(state,'turn1','我只是想吐槽',bot.now())
        self.assertTrue(companion.expression_hint(state,'我心里很烦',bot.now()))
        self.assertFalse(companion.expression_hint(state,'我很烦，怎么办',bot.now()))
        state['state']['character']='另一个角色'
        self.assertFalse(companion.expression_hint(state,'我很烦',bot.now()))

    def test_recovery_with_real_workflow_structures(self):
        root=Path(__file__).parent
        count=0
        for name in ('workflow.json','workflow_h_api.json','workflow_h_before_detail_api.json'):
            graph=json.loads((root/name).read_text(encoding='utf-8-sig'))
            if 'nodes' in graph:
                graph={str(n['id']):{'class_type':n['type']} for n in graph['nodes']}
            original=next(k for k,n in graph.items() if n.get('class_type')=='SaveImage')
            graph['new-output']=graph.pop(original)
            result={'outputs':{'9':{'images':[{'filename':'preview','type':'temp'}]},
                               'new-output':{'images':[{'filename':'correct','type':'output'}]}}}
            self.assertEqual(output_images(result,graph)[0]['filename'],'correct')
            count+=1
        self.assertEqual(count,3)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_remediation.PipelineTests.asyncSetUp
    asyncTearDown=test_remediation.PipelineTests.asyncTearDown

    async def test_vent_omits_advice_and_finalizes_confirmed_prefix(self):
        response={**copy.deepcopy(self.data),'response_plan':{'segments':['我听着。','下次你可以先写个计划。'],
                  'segment_types':['reaction','advice'],'segment_priority':['essential','optional']}}
        model=AsyncMock(return_value=response)
        with patch.object(bot,'deepseek',model):
            await self.instance.handle(self.event,self.key,'我只是想吐槽，今天很烦。')
        state=self.instance.store.get(self.key)
        self.assertEqual([s['status'] for s in state['delivery_plan']['segments']],['sent','cancelled'])
        self.assertEqual(state['pending_turn']['status'],'acknowledged')
        self.assertEqual([m['content'] for m in state['history'] if m['role']=='assistant'],['我听着。'])
        self.assertEqual(model.await_count,1)
