import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import bot
import response_plan
import test_remediation
import turn_taking as turns


class ClockTests(unittest.TestCase):
    def test_quiet_deadline_moves_but_burst_deadline_does_not(self):
        cfg = turns.config()['input_collector']
        queue = [({'_queued_at': 10.0}, '我跟你说')]
        self.assertGreater(turns.remaining_window(queue, 10.0, cfg), 1.2)
        queue += [({'_queued_at': stamp}, '还有') for stamp in (10.8, 11.6, 12.5)]
        self.assertAlmostEqual(turns.remaining_window(queue, 12.5, cfg), .5)
        self.assertEqual(turns.remaining_window(queue, 13.0, cfg), 0)
        queue.append(({'_queued_at': 12.6}, '/取消'))
        self.assertEqual(turns.remaining_window(queue, 12.6, cfg), 0)

    def test_semantic_single_and_schema_fallback(self):
        text = '一段完整的回答。' * 20
        self.assertEqual(response_plan.parse({'reply': text})['segments'], [text])
        with self.assertRaises(ValueError):
            response_plan.parse({'response_plan': {'segments': ['嗯'], 'segment_types': ['WAIT']}})
        plan = response_plan.parse({'response_plan': {'segments': ['先算角度。', '再算距离。'],
                                                      'segment_types': ['answer', 'advice']}})
        ep = turns.episode(plan, '求助', None, turns.config({'staged_reply': {'enabled': False}}), bot.now())
        self.assertEqual(ep['mode'], 'SINGLE')
        self.assertEqual(ep['segments'][0]['text'], '先算角度。\n再算距离。')


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_remediation.PipelineTests.asyncSetUp
    asyncTearDown = test_remediation.PipelineTests.asyncTearDown

    def model_reply(self, segments):
        return {**copy.deepcopy(self.data), 'response_plan': {'segments': segments}}

    async def test_four_inputs_one_call_one_reaction(self):
        model = AsyncMock(return_value=self.model_reply(['哈哈哈哈。']))
        inputs = ['有件糗事', '英语课点我', '我脑子短路了', '全班都在看我']
        with patch.object(bot, 'deepseek', model):
            for text in inputs:
                self.instance.enqueue(self.event, self.key, text)
            await self.instance.inbox_workers[self.key]
        self.assertEqual(model.await_count, 1)
        self.assertEqual(model.call_args.args[3], '\n'.join(inputs))
        plan = self.instance.store.get(self.key)['delivery_plan']
        self.assertEqual((plan['version'], plan['mode']), (4, 'SINGLE'))
        self.assertEqual([p['text'] for p in plan['segments']], ['哈哈哈哈。'])

    async def test_ack_timeout_and_late_confirmation(self):
        self.instance.call = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch.object(bot, 'deepseek', AsyncMock(return_value=self.model_reply(['先说一句。', '再补一句。']))):
            await self.instance.handle(self.event, self.key, '我有点难过')
        session = self.instance.store.get(self.key)
        first, later = session['delivery_plan']['segments']
        self.assertEqual((first['status'], later['status']), ('unknown', 'cancelled'))
        self.assertEqual(session['last_input_emotion']['emotion'], 'sadness')
        self.assertEqual(self.instance.call.await_count, 1)
        for _ in range(2):
            self.instance.store.late_receipt(first['echo'], {'retcode': 0, 'data': {'message_id': 81}}, bot.now())
        session = self.instance.store.get(self.key)
        self.assertEqual(session['delivery_plan']['segments'][0]['status'], 'sent')
        self.assertEqual([m['content'] for m in session['history'] if m['role']=='assistant'], ['先说一句。'])

    async def test_restart_between_segments(self):
        session = self.instance.store.get(self.key)
        session['delivery_plan'] = {'id':'old', 'version':7, 'segments':[
            {'index':0, 'status':'sent'}, {'index':1, 'status':'sending'}, {'index':2, 'status':'queued'}]}
        self.instance.store.save(self.key, session)
        fresh = bot.Bot(None)
        try:
            restored = fresh.store.get(self.key)['delivery_plan']
            self.assertEqual([s['status'] for s in restored['segments']], ['sent','unknown','cancelled'])
            self.assertFalse(fresh.tasks)
        finally:
            fresh.store.db.close()

    async def test_two_chats_keep_separate_versions(self):
        model = AsyncMock(return_value=self.model_reply(['先把两边同除以二。']))
        with patch.object(bot, 'deepseek', model):
            for uid in (42, 43):
                self.instance.enqueue({**self.event, 'user_id':uid}, f'private:0:{uid}', '这道方程怎么解？')
            await asyncio.gather(*self.instance.inbox_workers.values())
        plans = [self.instance.store.get(f'private:0:{uid}')['delivery_plan'] for uid in (42,43)]
        self.assertEqual([p['version'] for p in plans], [1,1])
        self.assertNotEqual(plans[0]['id'], plans[1]['id'])
        self.assertEqual(model.await_count, 2)

    async def test_old_model_even_if_it_ignores_cancellation(self):
        started = asyncio.Event()
        async def model(http, state, history, text, *args, **kwargs):
            if text == '第一条':
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return self.model_reply(['过期正文'])
            return self.model_reply(['接着说。'])
        with patch.object(bot, 'deepseek', model):
            self.instance.enqueue(self.event, self.key, '第一条')
            await asyncio.wait_for(started.wait(), 2)
            self.instance.enqueue(self.event, self.key, '我还有个补充')
            await self.instance.inbox_workers[self.key]
        self.assertEqual([p['message'][0]['data']['text'] for _,p in self.packets], ['接着说。'])

    async def test_interruption_during_natural_pause(self):
        self.transport_preferences['turn_taking']['natural_pause'] = {'enabled':True, 'min_ms':4000, 'max_ms':4000}
        first_ack = asyncio.Event()
        async def ack(action, params, ws):
            text = params['message'][0]['data']['text']
            self.packets.append(text)
            first_ack.set()
            return {'message_id':len(self.packets)}
        self.instance.call = ack
        model = AsyncMock(side_effect=[self.model_reply(['我听着呢。','那就试试这个。']), self.model_reply(['原来是这样。'])])
        with patch.object(bot, 'deepseek', model):
            self.instance.enqueue(self.event, self.key, '有件事我想说')
            await asyncio.wait_for(first_ack.wait(), 2)
            self.instance.enqueue(self.event, self.key, '我只是想让你听')
            await asyncio.wait_for(self.instance.inbox_workers[self.key], 2)
        self.assertEqual(self.packets, ['我听着呢。','原来是这样。'])
        self.assertIn('我听着呢。', [m['content'] for m in model.call_args.args[2]])

    async def test_pause_failure_combines_only_unsent_segments(self):
        self.transport_preferences['turn_taking']['natural_pause']['enabled'] = True
        model = AsyncMock(return_value=self.model_reply(['一。','二。','三。']))
        with patch.object(bot, 'deepseek', model), patch.object(turns, 'pause_seconds', side_effect=ValueError):
            await self.instance.handle(self.event, self.key, '怎么做？')
        self.assertEqual([p['message'][0]['data']['text'] for _,p in self.packets], ['一。','二。\n三。'])

    async def test_rejected_transport_is_observed_failure(self):
        attempts = []
        async def reject(action, params, ws):
            attempts.append(params)
            raise bot.ImageFailure('qq_send_rejected','send','rejected')
        self.instance.call = reject
        with patch.object(bot, 'deepseek', AsyncMock(return_value=self.model_reply(['正文','补充']))):
            await self.instance.handle(self.event, self.key, '说说看')
        plan = self.instance.store.get(self.key)['delivery_plan']
        self.assertEqual([s['status'] for s in plan['segments']], ['failed','cancelled'])
        self.assertEqual(sum(p['message'][0]['data']['text']=='正文' for p in attempts), 1)
