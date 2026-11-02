import copy
from contextlib import closing
from datetime import datetime
import hashlib
import sqlite3
import tempfile
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

import bot
from companion import Companion, character_key
from visual_brief import assemble, cache_context, validate_brief
from workflow_validation import validate_graph


class IdentityTests(unittest.TestCase):
    def test_description_changes_keep_known_character_identity(self):
        self.assertEqual(character_key('铃木羽那，温柔开朗'), character_key('Suzuki Hana，蓝紫色长发'))
        self.assertNotEqual(character_key('原创角色小林'), character_key('铃木羽那'))

    def test_existing_visit_dates_are_migrated_without_double_counting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.db'
            service = Companion(path)
            character = bot.DEFAULT['character']
            legacy = hashlib.sha256(character.encode()).hexdigest()[:20]
            with closing(sqlite3.connect(path)) as db, db:
                for day in ('2026-10-26','2026-10-27','2026-10-28'):
                    db.execute('INSERT INTO visits VALUES (?,?,?,?)', ('42',legacy,day,day+'T10:00:00+08:00'))
            current = datetime(2026,10,30,14,tzinfo=ZoneInfo('Asia/Shanghai'))
            service.context('42',character,current)
            service.context('42','Suzuki Hana',current)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT DISTINCT character FROM visits').fetchall(),[(character_key(character),)])
                self.assertEqual([r[0] for r in db.execute('SELECT day FROM visits ORDER BY day')],['2026-10-26','2026-10-27','2026-10-28'])
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM visits').fetchone()[0],3)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.session = {'state': {'character':'原创小林，黑发', 'clothing':'米色开衫', 'location':'家里书桌'}}
        self.data = {'state':copy.deepcopy(self.session['state']), 'visual_tags':'legacy tags', 'visual_scene':{
            'identity':'black hair, brown eyes', 'outfit':'beige cardigan, white top', 'setting':'home desk, books',
            'action':'sorting books', 'composition':'side view, upper body'}}

    def test_same_outfit_and_setting_keep_vocabulary_while_action_changes(self):
        assemble(self.session, self.data)
        self.data['visual_scene'].update(identity='blonde hair',outfit='blue dress', setting='beach', action='holding a book')
        prompt = assemble(self.session, self.data)
        self.assertIn('beige cardigan', prompt)
        self.assertIn('home desk', prompt)
        self.assertIn('holding a book', prompt)
        self.assertIn('black hair', prompt)
        self.assertNotIn('blue dress', prompt)
        self.assertNotIn('blonde hair', prompt)
        self.assertIn('固定绘图标签', cache_context(self.session))

    def test_explicit_wardrobe_change_invalidates_only_outfit(self):
        assemble(self.session,self.data)
        self.data['state']['clothing'] = '蓝色连衣裙'
        self.data['visual_scene'].update(outfit='blue dress',setting='incorrect new location')
        prompt = assemble(self.session,self.data)
        self.assertIn('blue dress',prompt)
        self.assertIn('home desk',prompt)
        self.assertNotIn('beige cardigan',prompt)

    def test_different_character_does_not_inherit_previous_clothes(self):
        assemble(self.session,self.data)
        self.data['state']['character'] = '原创小春'
        self.data['visual_scene'].update(identity='red hair',outfit='green shirt')
        prompt = assemble(self.session,self.data)
        self.assertIn('green shirt',prompt)
        self.assertNotIn('beige cardigan',prompt)

    def test_legacy_output_remains_usable_and_invalid_brief_is_rejected(self):
        self.data.pop('visual_scene')
        self.assertEqual(assemble(self.session,self.data),'legacy tags')
        self.assertNotIn('visual_cache',self.session)
        with self.assertRaises(ValueError):
            validate_brief({'outfit':['not','a','string']})


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.info={'Source':{'input':{'required':{'model':[['installed.safetensors']]}},'output':['IMAGE']},
                   'Save':{'input':{'required':{'images':['IMAGE']}}}}
        self.graph={'1':{'class_type':'Source','inputs':{'model':'installed.safetensors'}},
                    '2':{'class_type':'Save','inputs':{'images':['1',0]}}}

    def test_valid_graph_and_missing_model(self):
        validate_graph(self.graph,self.info)
        self.graph['1']['inputs']['model']='missing.safetensors'
        with self.assertRaisesRegex(ValueError,'模型文件名'):
            validate_graph(self.graph,self.info)

    def test_invalid_links_and_cycles(self):
        self.graph['2']['inputs']['images']=['missing',0]
        with self.assertRaisesRegex(ValueError,'不存在'):
            validate_graph(self.graph,self.info)
        self.graph['2']['inputs']['images']=['1',3]
        with self.assertRaisesRegex(ValueError,'越界'):
            validate_graph(self.graph,self.info)
        self.graph['2']['inputs']['images']=['2',0]
        with self.assertRaisesRegex(ValueError,'循环'):
            validate_graph(self.graph,self.info)


if __name__=='__main__':
    unittest.main()
