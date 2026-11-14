import asyncio
import copy
import unittest
from datetime import datetime
from image_director import PrioritySlots
import delivery_state

class GPUQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_releases_waiter_without_releasing_active_slot(self):
        slots=PrioritySlots(wait_timeout=.02)
        async with slots.slot(0):
            with self.assertRaises(asyncio.TimeoutError):
                async with slots.slot(1):self.fail('active GPU was stolen')
            self.assertTrue(slots.active)
            self.assertEqual(slots.waiters,[])
        async with slots.slot(0):self.assertTrue(slots.active)
        self.assertFalse(slots.active)

    async def test_capacity_rejects_without_unbounded_waiters(self):
        slots=PrioritySlots(max_waiters=1,wait_timeout=1)
        async def waiting():
            async with slots.slot(1):pass
        async with slots.slot(0):
            task=asyncio.create_task(waiting());await asyncio.sleep(.01)
            with self.assertRaisesRegex(RuntimeError,'GPU_QUEUE_FULL'):
                async with slots.slot(2):pass
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(slots.waiters,[])

class AckTests(unittest.TestCase):
    def test_unknown_and_duplicate_receipts_do_not_commit_twice(self):
        current=datetime.now();session={'state':{},'history':[]}
        data={'state':{}}
        delivery_state.stage(session,data,{},'input',1,current)
        delivery_state.acknowledge(session,data,'hello','user',None,current)
        self.assertEqual(session['history'],[])
        self.assertEqual(session['pending_turn']['status'],'outcome_unknown')
        delivery_state.acknowledge(session,data,'hello','user',{'message_id':7},current)
        delivery_state.acknowledge(session,data,'hello','user',{'message_id':7},current)
        self.assertEqual(len(session['history']),1)
