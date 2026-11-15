import asyncio,tempfile,unittest
from pathlib import Path
from datetime import datetime
from task_coordinator import TaskCoordinator,LaneBusy
from state_committer import StateCommitter
import bot,delivery_state

class LaneTests(unittest.IsolatedAsyncioTestCase):
 async def test_busy_background_does_not_block_text_or_control(self):
  c=TaskCoordinator();release=asyncio.Event();started=asyncio.Event()
  async def work():started.set();await release.wait()
  tasks=[asyncio.create_task(c.run('background',work)) for _ in range(2)]
  await started.wait();await asyncio.sleep(0)
  async def immediate():return 7
  self.assertEqual(await asyncio.wait_for(c.run('text',immediate),.2),7)
  self.assertEqual(await asyncio.wait_for(c.run('control',immediate),.2),7)
  release.set();await asyncio.gather(*tasks)
  self.assertEqual(sum(c.active.values()),0)
 async def test_cancel_waiter_releases_accounting(self):
  c=TaskCoordinator();c.semaphores['text']=asyncio.Semaphore(0)
  task=asyncio.create_task(c.run('text',lambda:asyncio.sleep(0)))
  await asyncio.sleep(.01);task.cancel();await asyncio.gather(task,return_exceptions=True)
  self.assertEqual(c.pending['text'],0)
 async def test_queue_full_does_not_construct_coroutine(self):
  c=TaskCoordinator();c.pending['text']=64
  with self.assertRaises(LaneBusy):await c.run('text',lambda:self.fail('must not create work'))

class CommitTests(unittest.TestCase):
 def test_late_receipt_cannot_commit_replacement_turn(self):
  with tempfile.TemporaryDirectory() as root:
   c=StateCommitter(bot.Store(Path(root)/'sessions.db'),TaskCoordinator())
   try:
    s=c.get('k');delivery_state.stage(s,{'state':s['state']},{},'new',2,datetime.now());c.save('k',s)
    _,accepted=c.acknowledge('k','old',{'state':s['state']},'old reply','user',{'message_id':4},datetime.now(),True)
    self.assertFalse(accepted);self.assertEqual(c.get('k')['history'],[])
   finally:c.db.close()
 def test_unknown_and_duplicate_receipts_are_not_accepted_effects(self):
  with tempfile.TemporaryDirectory() as root:
   c=StateCommitter(bot.Store(Path(root)/'sessions.db'),TaskCoordinator())
   try:
    s=c.get('k');d={'state':s['state']};delivery_state.stage(s,d,{},'i',1,datetime.now());c.save('k',s)
    for ack,expected in [(None,False),({'message_id':5},True),({'message_id':5},False)]:
     _,accepted=c.acknowledge('k','i',d,'reply','user',ack,datetime.now(),True);self.assertEqual(accepted,expected)
    self.assertEqual(len(c.get('k')['history']),1)
   finally:c.db.close()

class ToneTests(unittest.TestCase):
 def test_disagreement_and_quotes_are_preserved(self):
  import tone_guard
  self.assertFalse(tone_guard.issues('我不同意这个结论，证据还不够。'))
  self.assertFalse(tone_guard.issues('他说：“活该。”这话不合适。'))
  self.assertTrue(tone_guard.issues('低分不冤。'))
  self.assertEqual(tone_guard.fallback('低分不冤。考试延时确实影响安排。'),'考试延时确实影响安排。')

class RecoveryTests(unittest.TestCase):
 def test_affect_outbox_replays_after_crash_without_double_application(self):
  from life import Life
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/'sessions.db';life=Life(Path(root)/'life')
   c=StateCommitter(bot.Store(path),TaskCoordinator());s=c.get('k')
   d={'state':s['state'],'affect':{'joy':1}}
   delivery_state.stage(s,d,{},'event',1,datetime.now());c.save('k',s)
   c.acknowledge('k','event',d,'reply','user',{'message_id':5},datetime.now(),True,affect_user='u')
   item=c.get('k')['pending_affect']['event']
   life.mood('u',item['delta'],datetime.fromisoformat(item['at']),event_id='event')
   c.db.close() # crash after effect DB commit, before clearing outbox
   c=StateCommitter(bot.Store(path),TaskCoordinator())
   try:
    c.recover(life);self.assertFalse(c.get('k')['pending_affect'])
    with life.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM committed_affect').fetchone()[0],1)
   finally:c.db.close()
