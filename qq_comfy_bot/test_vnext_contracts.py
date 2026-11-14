import unittest, tempfile
from pathlib import Path
from datetime import datetime
from life import Life
from input_ledger import InputLedger
from context_snapshot import ContextSnapshot
import chat_analysis

class Contracts(unittest.TestCase):
    def test_snapshot_detached(self):
        session={'state':{'clothing':'white'},'scene_version':2}
        snap=ContextSnapshot.capture('private:0:1','input',session,[], 'context',{})
        session['state']['clothing']='black'
        view=snap.slice(); view['state']['clothing']='red'
        self.assertEqual(snap.slice()['state']['clothing'],'white')
        self.assertNotEqual(snap.snapshot_id,ContextSnapshot.capture('private:0:2','input',session,[],'context',{}).snapshot_id)

    def test_ledger_restart_and_scope(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'inputs.db'; ledger=InputLedger(p)
            i=ledger.receive('private:0:1',{'message_id':7},'private text')
            ledger.transition(i,'processing','STARTED')
            ledger=InputLedger(p)
            self.assertEqual(ledger.diagnostics()[0]['status'],'outcome_unknown')
            self.assertIsNone(ledger.receive('private:0:1',{'message_id':7},'private text'))
            self.assertIsNotNone(ledger.receive('group:2:1',{'message_id':7},'group text'))
            self.assertNotIn('private text',str(ledger.diagnostics()))

    def test_fabricated_value_all_kinds_and_readonly_mood(self):
        with tempfile.TemporaryDirectory() as root:
            life=Life(root); current=datetime.now()
            for kind in ('preference','activity','plan','basic_fact'):
                self.assertEqual(life.record('1',[{'kind':kind,'value':'火星','evidence':'我喜欢看书'}],'我喜欢看书',current),[])
            life.mood('1',{},current)
            with life.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM feelings').fetchone()[0],0)
            life.mood('1',{'joy':1},current)
            with life.db() as db:before=db.execute('SELECT * FROM feelings').fetchall()
            life.mood('1',{},current)
            with life.db() as db:self.assertEqual(db.execute('SELECT * FROM feelings').fetchall(),before)

    def test_semantic_routing(self):
        self.assertTrue(chat_analysis.should_split('你是不是讨厌我'))
        self.assertTrue(chat_analysis.should_split('你还记得那天吗'))
        self.assertFalse(chat_analysis.should_split('哈哈'*200))
        self.assertFalse(chat_analysis.should_split('/取消'))
