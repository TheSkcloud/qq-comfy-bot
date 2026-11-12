import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import aiohttp
from aiohttp import web

import bot


def workflow_info(workflow):
    """Mock backend node schemas, with enums to catch unavailable models."""
    info = {}
    for node in workflow['nodes']:
        required = {}
        for key, value in node.get('widgets_values_named', {}).items():
            if key == 'control_after_generate':
                continue
            required[key] = [[value]] if key.endswith('_name') or key in ('type', 'weight_dtype', 'device') else ['STRING']
        for socket in node.get('inputs', []):
            required[socket['name']] = [socket['type']]
        previous = info.get(node['type'], {}).get('input', {}).get('required', {})
        for name, spec in required.items():
            if isinstance(spec[0], list) and name in previous:
                spec[0] = list(dict.fromkeys(previous[name][0] + spec[0]))
        info[node['type']] = {'input': {'required': required}}
    return info


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads((bot.ROOT / 'workflow.json').read_text(encoding='utf-8-sig'))
        self.info = workflow_info(self.workflow)

    def test_conversion_preserves_graph_and_removes_ui_prompt_links(self):
        before = copy.deepcopy(self.workflow)
        graph = bot.convert(self.workflow, self.info)
        self.assertEqual(before, self.workflow)
        self.assertNotIn('139', graph)
        self.assertNotIn('143', graph)
        self.assertEqual(graph['67']['inputs']['text'], '')
        self.assertEqual(graph['68']['inputs']['batch_size'], 1)
        self.assertEqual(graph['70']['inputs']['denoise'], 1.0)
        self.assertEqual(graph['70']['inputs']['cfg'], 1.2)
        self.assertEqual(graph['70']['inputs']['scheduler'], 'simple')
        self.assertNotIn('control_after_generate', graph['70']['inputs'])
        self.assertEqual(graph['107']['inputs']['lora_name'], 'Blue_archive_style.safetensors')
        self.assertEqual(graph['109']['inputs']['lora_name'], 'Hand-drawnfeel-000018.safetensors')
        self.assertEqual(graph['109']['inputs']['strength_model'], 0.65)
        self.assertEqual(graph['150']['inputs']['strength_model'], -0.5)
        self.assertEqual(graph['9']['inputs']['images'], ['65', 0])
        for node in graph.values():
            for value in node['inputs'].values():
                if isinstance(value, list):
                    self.assertIn(value[0], graph)

    def test_missing_plugin_and_model_are_actionable(self):
        self.info.pop('VAELoader')
        with self.assertRaisesRegex(ValueError, 'VAELoader'):
            bot.convert(self.workflow, self.info)
        self.info = workflow_info(self.workflow)
        self.info['UNETLoader']['input']['required']['unet_name'] = [['missing.safetensors']]
        with self.assertRaisesRegex(ValueError, '模型文件名'):
            bot.convert(self.workflow, self.info)

    def test_store_survives_restart_and_isolates_users(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            store = bot.Store(path)
            s = store.get('alice')
            s['state']['clothing'] = 'white dress'
            store.save('alice', s)
            store.db.close()
            store = bot.Store(path)
            self.assertEqual(store.get('alice')['state']['clothing'], 'white dress')
            self.assertNotEqual(store.get('bob')['state']['clothing'], 'white dress')
            store.db.close()

    def test_prompt_uses_approved_h_style(self):
        prompt = bot.visual_prompt('white dress, beach, HDR, @BlueArchStyle', 13)
        self.assertIn('white dress', prompt)
        self.assertIn('(@ningen mame:1.9)', prompt)
        self.assertIn('scnr', prompt)
        self.assertNotIn('HDR', prompt)
        self.assertNotIn('@BlueArchStyle', prompt)

    def test_mood_and_daily_appearance(self):
        self.assertEqual(bot.mood_picture('语气更轻快，但不夸张刷屏。')[1], 3)
        self.assertEqual(bot.mood_picture('语气稍低缓')[1], 1)
        self.assertEqual(bot.daily_appearance('2026-10-29', '铃木羽那'), bot.daily_appearance('2026-10-29', '铃木羽那'))
        self.assertEqual(bot.daily_appearance('2026-10-29', '其他角色'), '')

    def test_read_and_reply_delay_accounts_for_elapsed_model_time(self):
        self.assertGreater(bot.response_delay(100, 100, 0), bot.response_delay(3, 3, 0))
        self.assertEqual(bot.response_delay(10, 10, 30), 0)
        self.assertLessEqual(bot.response_delay(1000, 1000, 0), 12)

    def test_natural_photo_requests(self):
        self.assertTrue(bot.image_requested('照片🥺'))
        self.assertTrue(bot.image_requested('我看看👀'))
        self.assertTrue(bot.image_requested('给我看看'))
        self.assertFalse(bot.image_requested('我看看代码再说'))
        self.assertFalse(bot.image_requested('正在学习使用gpt'))


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.timing_patch=patch('reply_scheduler.ReplyScheduler.remaining',return_value=0)
        self.timing_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        workflow = json.loads((bot.ROOT / 'workflow.json').read_text(encoding='utf-8-sig'))
        (self.root / 'workflow.json').write_text(json.dumps(workflow), encoding='utf-8')
        self.info = workflow_info(workflow)
        self.requests = []
        self.reply = dict(reply='好呀，去海边吧。', state=dict(character='成年女性，金发绿眼',
                          clothing='白色连衣裙', location='海边'), send_image=True,
                          visual_tags='1woman, blonde hair, green eyes, white dress, beach')
        self.reply['visual_scene']=dict(identity='woman',outfit='white dress',setting='beach',action='standing',composition='medium shot')
        from test_planning_fixture import creative
        self.reply['creative_brief']=creative('standing beside the sea')
        self.qa_patch=patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True,'test_stub':True}))
        self.qa_patch.start()
        self.ds_calls = 0
        self.ds_bad_once = False
        self.comfy_error = False
        self.disconnect_once = False
        self.ws_messages = asyncio.Queue()
        app = web.Application()

        async def info(request):
            return web.json_response(self.info)

        async def system_stats(request):
            return web.json_response({'system': {}})

        async def prompt(request):
            self.requests.append(await request.json())
            return web.json_response({'prompt_id': 'fake-id', 'node_errors': {}})

        async def history(request):
            if self.comfy_error:
                return web.json_response({'fake-id': {'status': {'status_str': 'error', 'messages': ['mock failure']}}})
            return web.json_response({'fake-id': {'outputs': {'9': {'images': [
                {'filename': 'test.png', 'subfolder': '', 'type': 'output'}]}}, 'status': {'completed': True}}})

        async def image(request):
            return web.Response(body=b'fake-png', content_type='image/png')

        async def ds(request):
            self.ds_calls += 1
            body = await request.json()
            self.assertIn('现实时间', body['messages'][0]['content'])
            self.assertEqual(body['response_format']['type'], 'json_object')
            content = '' if self.ds_bad_once and self.ds_calls == 1 else json.dumps(self.reply)
            return web.json_response({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})

        async def ws_endpoint(request):
            self.assertEqual(request.headers.get('Authorization'), 'Bearer test-token')
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            # Server emits an incoming QQ event, receives outgoing send API and echoes ack.
            await ws.send_json(dict(post_type='message', message_type='private', user_id=42,
                                    self_id=99, message_id=1,
                                    message=[{'type': 'text', 'data': {'text': '换上白色连衣裙，去海边拍照'}}]))
            async for msg in ws:
                packet = json.loads(msg.data)
                if packet['action'] == 'set_input_status':
                    await ws.send_json(dict(status='ok', retcode=0, data=None, echo=packet['echo']))
                    continue
                await self.ws_messages.put(packet)
                await ws.send_json(dict(status='ok', retcode=0, data={'message_id': 100}, echo=packet['echo']))
                if self.disconnect_once:
                    self.disconnect_once = False
                    await ws.close()
                    break
            return ws

        app.router.add_get('/object_info', info)
        app.router.add_get('/system_stats', system_stats)
        app.router.add_post('/prompt', prompt)
        app.router.add_get('/history/{pid}', history)
        app.router.add_get('/view', image)
        app.router.add_post('/chat/completions', ds)
        app.router.add_get('/ws', ws_endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.env = patch.dict(os.environ, dict(COMFYUI_URL=self.url, DEEPSEEK_BASE_URL=self.url,
                         DEEPSEEK_API_KEY='test', NAPCAT_TOKEN='test-token',
                         NAPCAT_WS_URL=f'ws://127.0.0.1:{port}/ws', ALLOWED_USERS='42',
                         ALLOWED_GROUPS='123', IMAGE_MODE='off', IMAGE_COOLDOWN='120',CONTEXT_ENABLED='0'))
        self.env.start()
        self.root_patch = patch.object(bot, 'ROOT', self.root)
        self.root_patch.start()
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        self.qa_patch.stop()
        self.timing_patch.stop()
        await self.http.close()
        await self.runner.cleanup()
        self.root_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_model_cannot_rewrite_identity_during_ordinary_chat(self):
        self.reply['state']['character'] = '不相关的新角色'
        result = await bot.deepseek(self.http,bot.DEFAULT,[],'你好')
        self.assertEqual(result['state']['character'],bot.DEFAULT['character'])
        result = await bot.deepseek(self.http,bot.DEFAULT,[],'角色换成不相关的新角色')
        self.assertEqual(result['state']['character'],'不相关的新角色')

    async def test_deepseek_empty_json_retry(self):
        self.ds_bad_once = True
        with patch.dict(os.environ,{'CHAT_SCHEMA_RETRY':'1'}):
            result = await bot.deepseek(self.http, bot.DEFAULT, [], '去海边拍照')
        self.assertEqual(result['state']['location'], '海边')
        self.assertEqual(self.ds_calls, 2)

    async def test_comfy_execution_error(self):
        self.comfy_error = True
        with self.assertRaisesRegex(RuntimeError, '执行失败'):
            await bot.Comfy(self.http).image('test')

    async def test_end_to_end_qq_chat_and_image(self):
        instance = bot.Bot(self.http)
        task = asyncio.create_task(instance.run())
        try:
            packets = [await asyncio.wait_for(self.ws_messages.get(), 8) for _ in range(2)]
            self.assertTrue(all(p['action'] == 'send_private_msg' for p in packets))
            self.assertEqual(packets[0]['params']['user_id'], 42)
            self.assertFalse(any('ComfyUI' in p['params']['message'][0]['data'].get('text', '') for p in packets))
            image = packets[1]['params']['message'][0]
            self.assertEqual(image['type'], 'image')
            self.assertTrue(image['data']['file'].startswith('base64://'))
            graph = self.requests[0]['prompt']
            self.assertIn('white dress', graph['67']['inputs']['text'])
            self.assertEqual(graph['68']['inputs']['batch_size'], 1)
            self.assertEqual(instance.store.get('private:0:42')['state']['location'], '海边')
            receipt = json.loads(next((self.root/'data'/'images').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual(receipt['status'],'completed')
            self.assertEqual(receipt['seed'],graph['70']['inputs']['seed'])
            self.assertEqual(receipt['graph'],graph)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            instance.store.db.close()

    async def test_render_survives_websocket_reconnect(self):
        self.disconnect_once = True
        instance = bot.Bot(self.http)
        task = asyncio.create_task(instance.run())
        try:
            first = await asyncio.wait_for(self.ws_messages.get(), 8)
            second = await asyncio.wait_for(self.ws_messages.get(), 10)
            self.assertEqual(first['params']['message'][0]['type'], 'text')
            self.assertEqual(second['params']['message'][0]['type'], 'image')
            self.assertEqual(len(self.requests), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            instance.store.db.close()

    async def test_explicit_request_draws_even_if_model_says_no(self):
        self.reply['send_image'] = False
        instance = bot.Bot(self.http)
        task = asyncio.create_task(instance.run())
        try:
            await asyncio.wait_for(self.ws_messages.get(), 8)
            packet = await asyncio.wait_for(self.ws_messages.get(), 8)
            self.assertEqual(packet['params']['message'][0]['type'], 'image')
            self.assertEqual(len(self.requests), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            instance.store.db.close()

    async def test_authorization_group_trigger_and_dedup(self):
        instance = bot.Bot(self.http)
        try:
            e = dict(post_type='message', message_type='group', user_id=42, self_id=99,
                     group_id=123, message_id=2, message=[{'type': 'text', 'data': {'text': '你好'}}])
            self.assertIsNone(instance.accept(e))
            e['message'].insert(0, {'type': 'at', 'data': {'qq': '99'}})
            self.assertIsNotNone(instance.accept(e))
            self.assertIsNone(instance.accept(e))
            e['user_id'] = 66
            self.assertIsNone(instance.accept(e))
        finally:
            instance.store.db.close()


if __name__ == '__main__':
    unittest.main()
