import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot
from companion import Companion, character_context
from daily_routine import DailyRoutine
from image_policy import allow_spontaneous, image_requested, picture_count
from life import Life
import preferences
from context_engine import ContextEngine

NOW = datetime(2026, 10, 30, 14, 10, tzinfo=ZoneInfo('Asia/Shanghai'))


class ContinuityTests(unittest.TestCase):
    def test_same_day_chat_does_not_unlock_relationship(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Companion(Path(folder)/'companion.db')
            for _ in range(30):
                service.touch('42', 'Hana', NOW)
            self.assertIn('了解仍有限', service.context('42', 'Hana', NOW))
            for n in range(1, 6):
                service.touch('42', 'Hana', NOW+timedelta(days=n))
            self.assertIn('了解仍有限', service.context('42', 'Hana', NOW+timedelta(days=6)))
            service.touch('42', 'Hana', NOW+timedelta(days=6))
            self.assertIn('已有一些熟悉感', service.context('42', 'Hana', NOW+timedelta(days=6)))
            self.assertIn('了解仍有限', service.context('42', 'another character', NOW))
            self.assertIn('了解仍有限', service.context('99', 'Hana', NOW))

    def test_later_stages_persist_without_offline_penalty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'companion.db'
            service = Companion(path)
            for n in range(21):
                service.touch('42', bot.DEFAULT['character'], NOW+timedelta(days=n))
            self.assertIn('相互信任', service.context('42', bot.DEFAULT['character'], NOW+timedelta(days=21)))
            for n in range(21,45):
                service.touch('42', bot.DEFAULT['character'], NOW+timedelta(days=n))
            self.assertIn('亲近但独立', Companion(path).context('42', bot.DEFAULT['character'], NOW+timedelta(days=90)))
            self.assertIn('用户已明确选择恋人模式', service.context('new', bot.DEFAULT['character'], NOW, 'partner'))

    def test_default_persona_migration_preserves_identity_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = bot.Store(Path(folder)/'sessions.db')
            old = {'state':dict(bot.DEFAULT, character=bot.LEGACY_WARM_CHARACTER, clothing='白色衬衫'),
                   'history':[{'role':'user','content':'共同话题'}]}
            store.save('old', old)
            migrated = store.get('old')
            self.assertEqual(migrated['state']['character'], bot.DEFAULT['character'])
            self.assertEqual(migrated['state']['clothing'], '白色衬衫')
            self.assertEqual(migrated['history'], old['history'])
            from companion import character_key
            self.assertEqual(character_key(bot.LEGACY_WARM_CHARACTER), character_key(bot.DEFAULT['character']))
            store.save('custom', dict(old, state=dict(old['state'],character='自定义角色')))
            self.assertEqual(store.get('custom')['state']['character'], '自定义角色')
            store.db.close()

    def test_anniversary_is_explicit_persisted_and_user_local(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'companion.db'
            service = Companion(path)
            service.anniversary('42', '初次聊天', '10-30')
            self.assertIn('初次聊天', Companion(path).context('42', 'Hana', NOW))
            self.assertNotIn('初次聊天', service.context('99', 'Hana', NOW))
            self.assertNotIn('初次聊天', service.context('42', 'Hana', NOW+timedelta(days=1)))
            with self.assertRaises(ValueError):
                service.anniversary('42', 'bad', '02-30')
            service.forget('42', '初次聊天')
            self.assertEqual(service.anniversary('42'), [])

    def test_lore_is_relevant_and_does_not_leak_between_characters(self):
        self.assertNotIn('写真服务', character_context(bot.DEFAULT['character'], '早上好'))
        self.assertIn('写真服务', character_context(bot.DEFAULT['character'], '照片'))
        self.assertNotIn('铃木羽那', character_context('原创角色，小林', '照片'))

    def test_routine_is_stable_but_phase_advances(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DailyRoutine(Path(folder)/'routine.db')
            first = service.current('42', NOW, {'summary':'有雨'})
            self.assertEqual(first, DailyRoutine(service.path).current('42', NOW, {'summary':'有雨'}))
            self.assertNotEqual(first, service.current('42', NOW+timedelta(minutes=70), {'summary':'有雨'}))
            agenda = service.agenda('42', NOW)
            self.assertEqual(agenda[0][0], 0)
            self.assertEqual(agenda[-1][1], 24)
            self.assertTrue(all(a[1] == b[0] for a,b in zip(agenda,agenda[1:])))


class RecallTests(unittest.TestCase):
    def test_relevance_beats_truncation_and_stale_activity_is_not_current(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Life(folder)
            for n in range(15):
                text = f'我喜欢编号{n}的事物'
                service.record('42', [{'kind':'preference','value':text,'evidence':text}], text, NOW)
            text = '我在图书馆看书'
            service.record('42', [{'kind':'activity','value':'看书','evidence':text}], text, NOW-timedelta(days=2))
            text = '我不喜欢咖啡，喜欢热茶'
            service.record('42', [{'kind':'preference','value':'喜欢热茶','evidence':text}], text, NOW-timedelta(days=1))
            result = service.recall('42', '你觉得热茶怎么样', NOW)
            self.assertIn('喜欢热茶', result)
            self.assertNotIn('看书', result)
            self.assertIn('2026-10-29', result)

    def test_exact_forget_removes_fact_journal_and_old_value(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Life(folder)
            for value in ('太原', '北京'):
                text = '我在'+value
                service.record('42', [{'kind':'location','value':value,'evidence':text}], text, NOW)
            text = '我在太原'
            service.record('99', [{'kind':'location','value':'太原','evidence':text}], text, NOW)
            service.forget('42', '太原')
            self.assertIsNone([e for e in service.journal() if e['value']=='北京'][0]['previous'])
            for line in (Path(folder)/'conversation_journal.jsonl').read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                if event['user']=='42':
                    self.assertNotIn('太原', str(event))
            self.assertEqual(service.facts('99')[0]['value'], '太原')


class ImagePolicyTests(unittest.TestCase):
    def test_declining_images_does_not_trigger_generation(self):
        for text in ('不要发图', '我不想看照片', '先不拍照', '别给我看照片了'):
            self.assertFalse(image_requested(text), text)
        self.assertTrue(image_requested('照片🥺'))
        self.assertEqual(picture_count('拍三张照片'), 3)
        self.assertEqual(picture_count('照片'), 1)

    def test_auto_images_respect_clock_busy_topics_and_persisted_quota(self):
        self.assertTrue(allow_spontaneous({}, '刚买了杯茶', NOW))
        self.assertFalse(allow_spontaneous({}, '我去写作业了', NOW))
        self.assertFalse(allow_spontaneous({}, '你好', NOW.replace(hour=1)))
        self.assertFalse(allow_spontaneous({}, '你好', NOW, False))
        self.assertFalse(allow_spontaneous({'last_image_sent_at':(NOW-timedelta(minutes=44)).isoformat()}, '你好', NOW))
        stamps = [(NOW-timedelta(hours=n)).isoformat() for n in (1,2,3)]
        self.assertFalse(allow_spontaneous({'image_events':stamps}, '你好', NOW))
        self.assertTrue(allow_spontaneous({'image_events':stamps}, '你好', NOW+timedelta(days=1)))

    def test_identity_lighting_and_style_are_separate(self):
        prompt = bot.visual_prompt('blonde hair, green eyes, white dress, daytime, sunset, reading', 22, character=bot.DEFAULT['character'])
        self.assertIn('golden eyes', prompt)
        self.assertIn('white dress', prompt)
        self.assertIn('night', prompt)
        self.assertNotIn('sunset', prompt)
        self.assertNotIn('blonde hair', prompt)
        self.assertIn('(@ningen mame:1.9)', prompt)
        custom = bot.visual_prompt('black hair, blue eyes', 10, character='原创黑发角色')
        self.assertIn('black hair', custom)
        self.assertNotIn('suzuki hana', custom)


class PreferenceTests(unittest.TestCase):
    def test_partial_update_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(preferences, 'PATH', Path(folder)/'preferences.json'):
            preferences.save({'romance_mode':'friends','notes_auto':False,'spontaneous_images':False})
            preferences.save({'reactions':False})
            saved = preferences.load()
            self.assertEqual(saved['romance_mode'], 'friends')
            self.assertFalse(saved['notes_auto'])
            self.assertFalse(saved['spontaneous_images'])
            with self.assertRaises(ValueError):
                preferences.save({'romance_mode':'unknown'})


class InboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_world_context_is_recalled_only_when_relevant(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'CONTEXT_ENABLED':'1'}), patch.object(preferences, 'load', return_value={'notes_auto':False}):
            engine = ContextEngine(folder)
            engine.weather.ensure = AsyncMock(return_value={'summary':'合成天气测试'})
            engine.routine.ensure = AsyncMock()
            with patch('context_engine.is_owner', return_value=False):
                casual = await engine.build(object(), '42', '我去写作业了', NOW, character=bot.DEFAULT['character'])
                scene = await engine.build(object(), '42', '你在做什么，照片', NOW, character=bot.DEFAULT['character'])
            self.assertNotIn('虚拟角色今天的日程', casual)
            self.assertNotIn('合成天气测试', casual)
            self.assertIn('虚拟角色今天的日程', scene)
            self.assertIn('合成天气测试', scene)

    async def test_bursts_are_combined_without_combining_commands(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, 'ROOT', Path(folder)):
            instance = bot.Bot(None)
            instance.handle = AsyncMock()
            event = {'message_type':'private','user_id':42}
            instance.enqueue(event, 'k', '今天去买茶')
            instance.enqueue(event, 'k', '结果店关门了')
            instance.enqueue(event, 'k', '/状态')
            await asyncio.gather(*list(instance.tasks))
            self.assertEqual([c.args[2] for c in instance.handle.await_args_list], ['今天去买茶\n结果店关门了', '/状态'])
            self.assertEqual(instance.inbox, {})
            instance.store.db.close()


if __name__ == '__main__':
    unittest.main()
