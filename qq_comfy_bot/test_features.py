import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
import dialogue


class ClockTests(unittest.TestCase):
    def test_current_clock_overrides_old_evening_across_midnight(self):
        current = datetime(2026, 10, 29, 0, 1, 2, tzinfo=ZoneInfo('Asia/Shanghai'))
        history = [{'role': 'user', 'content': '晚上好', 'at': '2026-10-28T23:59:00+08:00'}]
        text = dialogue.clock_context(current, history)
        self.assertIn('2026-10-29 00时', text)
        self.assertNotIn('秒', text)
        self.assertIn('凌晨', text)

    def test_send_time_replaces_hallucinated_clock(self):
        current = datetime(2026, 10, 28, 19, 30, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(dialogue.render_time('现在是七点半。', current), '现在是19时。')
        self.assertEqual(dialogue.render_time('现在是08:00。', current), '现在是19时。')
        self.assertEqual(dialogue.render_time('现在是上午。', current), '现在是晚上。')

    def test_history_metadata_is_not_sent_as_unknown_api_fields(self):
        output = dialogue.api_history([dict(role='user', content='你好', at='2026-10-28T19:30:12+08:00'),
                                       dict(role='assistant', content='嗨')])
        self.assertEqual(set(output[0]), {'role', 'content'})
        self.assertIn('[历史]', output[1]['content'])
        self.assertTrue(dialogue.time_question('现在几点了？'))
        self.assertFalse(dialogue.time_question('你说的那个时间安排不错'))


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch_root = patch.object(bot, 'ROOT', self.root)
        self.patch_root.start()
        self.reply = dict(reply='嗯，我在。', state=bot.DEFAULT.copy(), send_image=False,
                          visual_tags='adult woman, blue shirt', waiting_lines=['这一句接着刚才的话题。', '另一句短短的接话。'], reaction='none')
        self.reply['visual_scene']=dict(identity='adult woman',outfit='blue shirt',setting='indoors',action='sitting',composition='medium shot')
        from test_planning_fixture import creative
        self.reply.update(creative_brief=creative('sitting quietly'),_thinking_enabled=True)
        self.api = patch.object(bot, 'deepseek', AsyncMock(return_value=self.reply))
        self.deepseek = self.api.start()
        self.instance = bot.Bot(None)
        self.instance.connected.set()
        self.instance.send = AsyncMock(return_value={'message_id':100})
        self.future = asyncio.get_running_loop().create_future()
        async def draw(prompt,job=None):
            return await self.future
        self.instance.comfy.image = draw
        self.event = dict(message_type='private', user_id=42)
        self.key = 'private:0:42'
        self.prefs = patch.object(bot.preferences, 'load', return_value=dict(reactions=True, image_chat=True, image_chat_interval=3, image_chat_max=4,drawing=bot.image_director.config()))
        self.prefs.start()
        self.qa=patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True,'test_stub':True}))
        self.qa.start()

    async def asyncTearDown(self):
        tasks = list(self.instance.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.instance.store.db.close()
        self.api.stop()
        self.prefs.stop()
        self.qa.stop()
        self.patch_root.stop()
        self.temp.cleanup()

    async def test_time_is_answered_without_model_call(self):
        await self.instance.handle(self.event, self.key, '现在几点了？')
        self.deepseek.assert_not_called()
        self.assertTrue(self.instance.send.call_args.kwargs['clock'])

    async def test_can_talk_while_image_is_rendering(self):
        await asyncio.wait_for(self.instance.handle(self.event, self.key, '照片🥺'), 1)
        await asyncio.sleep(0)
        self.assertIn(self.key, self.instance.image_tasks)
        await asyncio.wait_for(self.instance.handle(self.event, self.key, '正在看一本书'), 1)
        self.assertEqual(self.deepseek.await_count, 2)
        self.future.set_result(b'png')
        await asyncio.gather(*list(self.instance.tasks))
        self.assertTrue(self.instance.store.get(self.key).get('last_image_sent_at'))

    async def test_followup_stops_after_user_interrupts(self):
        await self.instance.handle(self.event, self.key, '照片🥺')
        await asyncio.sleep(0)
        await self.instance.handle(self.event, self.key, '继续聊刚才的话题')
        await asyncio.sleep(3.1)
        texts = [c.args[1] for c in self.instance.send.call_args_list if len(c.args)>1]
        self.assertNotIn(self.reply['waiting_lines'][0], texts)

    async def test_followup_sent_during_wait_and_stops_on_completion(self):
        await self.instance.handle(self.event, self.key, '照片🥺')
        await asyncio.sleep(3.1)
        texts = [c.args[1] for c in self.instance.send.call_args_list if len(c.args)>1]
        self.assertNotIn(self.reply['waiting_lines'][0], texts)
        self.future.set_result(b'png')
        await asyncio.gather(*list(self.instance.tasks))
        texts = [c.args[1] for c in self.instance.send.call_args_list if len(c.args)>1]
        self.assertNotIn(self.reply['waiting_lines'][1], texts)

    async def test_explicit_photo_is_one_image_even_when_happy(self):
        self.instance.cooldowns[self.key] = __import__('time').monotonic()
        self.instance.comfy.image = AsyncMock(return_value=b'png')
        with patch.object(bot, 'mood_picture', return_value=('cheerful expression', 3)):
            await self.instance.handle(self.event, self.key, '照片🥺')
            await asyncio.gather(*list(self.instance.tasks))
        images = [call for call in self.instance.send.call_args_list if call.kwargs.get('image') == b'png']
        self.assertEqual(len(images), 1)
        self.assertEqual(self.instance.comfy.image.await_count, 1)

    async def test_notes_never_read_into_group(self):
        with patch.object(bot, 'read_notes', AsyncMock()) as reader:
            await self.instance.handle(dict(message_type='group', group_id=123, user_id=42), self.key, '/笔记 学习')
            reader.assert_not_called()
            self.assertIn('私聊', self.instance.send.call_args.args[1])

    async def test_reaction_card_has_cooldown(self):
        self.reply['reaction'] = 'happy'
        folder = self.root / 'assets' / 'reactions'
        folder.mkdir(parents=True)
        (folder/'happy.png').write_bytes(b'local-sticker')
        await self.instance.handle(self.event, self.key, '哈哈')
        await self.instance.handle(self.event, self.key, '真的好好笑')
        image_calls = [c for c in self.instance.send.call_args_list if c.kwargs.get('image')]
        self.assertEqual(len(image_calls), 1)


if __name__ == '__main__':
    unittest.main()
