import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import bot
import chat_analysis
import image_requests
import image_workflow
import shot_director
import test_remediation
import test_upgrade
from test_planning_fixture import creative


class ModelResponse(test_remediation.ProviderResponse):
    def __init__(self, content, started=None, release=None):
        super().__init__(json.dumps(content, ensure_ascii=False))
        self.started = started
        self.release = release

    async def __aenter__(self):
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        return self

    def raise_for_status(self):
        pass


class Model:
    def __init__(self, reply, metadata=None):
        self.reply = reply
        self.metadata = metadata or {}
        self.requests = []
        self.analysis_started = asyncio.Event()
        self.release_analysis = asyncio.Event()

    def post(self, url, *, json, **kwargs):
        self.requests.append(copy.deepcopy(json))
        if json['messages'][0]['content'].startswith(chat_analysis.SYSTEM):
            return ModelResponse(self.metadata, self.analysis_started, self.release_analysis)
        return ModelResponse(self.reply)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_upgrade.EntryTests.asyncSetUp
    asyncTearDown = test_upgrade.EntryTests.asyncTearDown
    image_data = test_upgrade.EntryTests.image_data

    def configure_model(self, reply, metadata=None):
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test', 'CHAT_SPLIT_MIN_CHARS': '40',
                                      'CHAT_SCHEMA_RETRY': '0', 'SPEECH_REVIEW_ENABLED': '0'})
        env.start()
        self.addCleanup(env.stop)
        model = Model(reply, metadata)
        self.instance.http = model
        return model

    async def test_short_chat_uses_one_request_without_drawing_instructions(self):
        model = self.configure_model({'reply': '蓝色。', 'send_image': True, 'visual_tags': 'a portrait'})
        self.instance.context.build.return_value = '用户习惯用浅蓝色的笔记封面。'
        await self.instance.handle(self.event, self.key, '你喜欢什么颜色？')
        self.assertEqual(len(model.requests), 1)
        system = model.requests[0]['messages'][0]['content']
        self.assertIn('浅蓝色的笔记封面', system)
        self.assertNotIn('visual_scene', system)
        self.assertNotIn('creative_brief', system)
        self.assertFalse(self.instance.image_queues)

    async def test_long_chat_uses_analysis_before_drafting_and_only_sends_the_draft(self):
        quote = '我喜欢用手账记录改稿'
        text = '最近我在整理小说手稿。' + quote + '。我给章节分了几个阶段，再按照顺序查看前后变化。'
        self.instance.context.build.return_value = '用户以前提过用页签追踪旧版本。'
        model = self.configure_model({'reply': '这样能看清每次改动。',
            'memory_updates': [{'kind': 'preference', 'value': '正文阶段捏造的偏好', 'evidence': quote}],
        }, {
            'reply': '这句话不应被发送',
            'memory_updates': [{'kind': 'preference', 'value': quote, 'evidence': quote}],
            'dialogue_update': {'relation': 'continue', 'topic_summary': '用手账整理小说改稿', 'evidence': quote},
            'reply_points': [{'text': '可以承接已有的版本页签习惯', 'evidence': '用页签追踪旧版本'},
                             {'text': '无依据的另一个偏好', 'evidence': '用户没有说过的内容'}],
        })
        pending = asyncio.create_task(self.instance.handle(self.event, self.key, text))
        await asyncio.wait_for(model.analysis_started.wait(), 1)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.packets, [])
        self.assertEqual(model.requests[0]['messages'][0]['content'], chat_analysis.SYSTEM)
        payload = json.loads(model.requests[0]['messages'][1]['content'])
        self.assertIn('用页签追踪旧版本', payload['local_context'])
        self.assertNotIn('acknowledged_reply', payload)
        self.assertEqual(self.instance.context.life.facts('42'), [])
        model.release_analysis.set()
        await asyncio.wait_for(pending, 1)
        self.assertEqual(len(model.requests), 2)
        system = model.requests[1]['messages'][0]['content']
        self.assertIn('用手账整理小说改稿', system)
        self.assertIn('可以承接已有的版本页签习惯', system)
        self.assertNotIn('无依据的另一个偏好', system)
        self.assertNotIn('这句话不应被发送', system)
        self.assertNotIn('memory_updates', system)
        self.assertEqual([fact['value'] for fact in self.instance.context.life.facts('42')], [quote])
        discussion = self.instance.store.get(self.key)['dialogue_state']
        self.assertEqual(discussion['topic_summary'], '用手账整理小说改稿')
        self.assertEqual(discussion['substantive_turns'], 1)
        self.assertEqual(len(self.packets), 1)
        self.assertEqual(self.packets[0][1]['message'][0]['data']['text'], '这样能看清每次改动。')

    async def test_new_turn_cancels_stale_metadata(self):
        quote = '我喜欢手写每一章的改动'
        model = self.configure_model({'reply': '明白。'}, {
            'memory_updates': [{'kind': 'preference', 'value': quote, 'evidence': quote}],
        })
        text = '我最近在整理长篇的手稿。' + quote + '。这次想把各个版本的修改顺序梳理得更清楚。'
        pending = asyncio.create_task(self.instance.handle(self.event, self.key, text))
        await asyncio.wait_for(model.analysis_started.wait(), 1)
        await asyncio.wait_for(self.instance.handle(self.event, self.key, '换个话题，说说颜色。'), 1)
        model.release_analysis.set()
        self.assertFalse(await asyncio.wait_for(pending, 1))
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[-1]['messages'][-1]['content'], '换个话题，说说颜色。')
        self.assertEqual(len(self.packets), 1)
        self.assertEqual(self.instance.context.life.facts('42'), [])

    async def test_invalid_analysis_falls_back_to_local_context_without_an_extra_retry(self):
        model = self.configure_model({'reply': '我记得你用过页签。'}, ['invalid analysis'])
        self.instance.context.build.return_value = '用户此前用页签区分手稿的版本。'
        model.release_analysis.set()
        await self.instance.handle(self.event, self.key, '我想把目前手稿各章的修改顺序再理一遍。' * 3)
        self.assertEqual(len(model.requests), 2)
        self.assertIn('用户此前用页签区分手稿的版本', model.requests[1]['messages'][0]['content'])
        self.assertEqual(self.packets[0][1]['message'][0]['data']['text'], '我记得你用过页签。')
        self.assertIn('memory_updates', model.requests[1]['messages'][0]['content'])

    async def test_invalid_draft_on_thinking_turn_stays_within_two_requests(self):
        model = self.configure_model({})
        model.release_analysis.set()
        with patch.dict(os.environ, {'CHAT_SCHEMA_RETRY': '1', 'SPEECH_REVIEW_ENABLED': '1'}):
            await self.instance.handle(self.event, self.key, '请深入分析我为什么要保留各版本的修改顺序。' * 3)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1]['thinking']['type'], 'disabled')
        self.assertIn('这次没完成', self.packets[0][1]['message'][0]['data']['text'])

    async def test_simple_photo_reuses_local_routine_without_visual_planning(self):
        model = self.configure_model({'reply': '给你看看。'})
        self.instance.comfy.image = AsyncMock(return_value=b'image')
        with patch('image_planner.complete_visual', AsyncMock()) as planner, \
                patch.object(bot.image_director, 'inspect_image', AsyncMock(return_value={'pass': True})):
            await self.instance.handle(self.event, self.key, '/拍照')
            await asyncio.wait_for(asyncio.gather(*list(self.instance.tasks)), 2)
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn('creative_brief', model.requests[0]['messages'][0]['content'])
        planner.assert_not_awaited()
        self.instance.comfy.image.assert_awaited_once()
        path = next((self.root/'data'/'image_jobs').glob('*/job.json'))
        job = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(job['snapshot']['visual_source'], 'local_routine')
        self.assertIn('reading an open book', job['prompt'])

    async def test_nonvisual_action_fails_before_gpu_submission(self):
        data = self.image_data()
        data['creative_brief'] = creative('Why draw this?')
        data['creative_brief']['gesture'] = data['creative_brief']['action']
        self.instance.comfy.image = AsyncMock()
        with patch.object(bot, 'deepseek', AsyncMock(return_value=data)):
            await self.instance.handle(self.event, self.key, '画一张看书的图')
        self.instance.comfy.image.assert_not_awaited()
        path = next((self.root/'data'/'image_jobs').glob('*/job.json'))
        job = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(job['reason'], 'prompt_quality_rejected')


class PromptTests(unittest.TestCase):
    def test_explicit_camera_visible_action_and_exclusions(self):
        from test_prompt_spec import scene

        request = '低机位手部特写，只画右手翻书，别戴帽子，拍张照给我'
        self.assertEqual(image_requests.classify(request, {}, bot.now())['action'], 'request')
        self.assertEqual(image_requests.classify('不用看代码', {}, bot.now())['action'], 'none')
        snapshot = scene()
        snapshot.update(action='turning a page with her right hand while glancing at the window',
                        outfit_tags='long sleeves, red hat', visual_exclusions=['hat'])
        snapshot['shot_plan'] = shot_director.choose(snapshot, request)
        profile = {}
        prompt = image_workflow.prompt(snapshot, profile)
        spec = profile['prompt_spec']
        self.assertIn('low angle', prompt)
        self.assertIn('one right hand', prompt)
        self.assertIn('turning a page with her right hand', prompt)
        self.assertNotIn('overhead', prompt)
        self.assertNotIn('red hat', prompt)
        self.assertNotIn('glancing', spec['action'])
        self.assertIn('hat', spec['scope']['excluded_objects'])

    def test_local_routine_varies_with_activity_and_keeps_saved_identity(self):
        from visual_brief import local_plan

        session = {'state': copy.deepcopy(bot.DEFAULT)}
        reading = local_plan(session, '在房间读书', bot.DEFAULT)
        walking = local_plan(session, '去附近散步', bot.DEFAULT)
        self.assertEqual(reading, local_plan(session, '在房间读书', bot.DEFAULT))
        self.assertEqual(reading['visual_scene']['identity'], walking['visual_scene']['identity'])
        self.assertNotEqual(reading['visual_scene']['outfit'], walking['visual_scene']['outfit'])
        self.assertNotEqual(reading['visual_scene']['setting'], walking['visual_scene']['setting'])
        self.assertEqual(session['state'], bot.DEFAULT)

    def test_explicit_image_only_identity_and_location_override_saved_role(self):
        from visual_brief import apply_request_options, assemble, complete_from_cache

        request = '画一张黑发男生在夜里的海边看书的图'
        session = {'state': copy.deepcopy(bot.DEFAULT)}
        data = {'state': copy.deepcopy(bot.DEFAULT), 'visual_scene': {
            'identity': 'one man, short black hair', 'outfit': 'plain shirt', 'setting': 'beach, ocean',
            'action': 'reading a book', 'composition': 'medium shot',
        }, 'visual_overrides': {'identity': '黑发男生', 'setting': '夜里的海边'}, 'visual_tags': ''}
        apply_request_options(data, request)
        self.assertEqual(complete_from_cache(session, data), [])
        assemble(session, data)
        snapshot = bot.image_director.freeze(session, data, {'style': 'life'}, bot.now(), {})
        snapshot['shot_plan'] = shot_director.choose(snapshot, '半身')
        prompt = image_workflow.prompt(snapshot, {})
        self.assertIn('one man, short black hair', prompt)
        self.assertIn('beach', prompt)
        self.assertIn('ambient artificial lighting', prompt)
        self.assertNotIn('one woman', prompt)
        self.assertNotIn('visual_cache', session)
        self.assertEqual(session['state'], bot.DEFAULT)
