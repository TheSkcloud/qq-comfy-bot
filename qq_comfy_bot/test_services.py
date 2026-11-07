import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import services
import manager


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        services._comfy_lock = asyncio.Lock()
        services._comfy_process = None
        # A source checkout has no private launcher.json or saved QQ login state.
        self.settings_patch=patch.object(services,'settings',return_value={'bot_qq':'test-account','napcat_dir':'.','python':'python'})
        self.settings_patch.start();self.addCleanup(self.settings_patch.stop)
        self.checkpoint_patch=patch.object(manager.login_checkpoint,'record')
        self.checkpoint_patch.start();self.addCleanup(self.checkpoint_patch.stop)

    async def test_existing_comfy_is_reused(self):
        with patch.object(services, 'comfy_ready', AsyncMock(return_value=True)), patch.object(services, 'spawn') as spawn:
            await services.ensure_comfy(None, 'http://127.0.0.1:8188')
            spawn.assert_not_called()

    async def test_concurrent_requests_launch_only_one_comfy(self):
        with tempfile.TemporaryDirectory() as folder:
            main = Path(folder) / 'main.py'
            main.touch()
            python = Path(folder) / 'python.exe'
            python.touch()
            config = dict(comfy_command=[str(python), '-s', str(main)], comfy_cwd=folder)
            started = [False]
            async def ready(*args):
                return started[0]
            def launch(*args):
                started[0] = True
                return Mock(poll=lambda: None)
            with patch.object(services, 'settings', return_value=config), patch.object(services, 'comfy_ready', ready), patch.object(services, 'spawn', side_effect=launch) as spawn:
                await asyncio.gather(services.ensure_comfy(None, 'http://localhost:8188'), services.ensure_comfy(None, 'http://localhost:8188'))
                self.assertEqual(spawn.call_count, 1)

    async def test_remote_service_is_not_spawned_locally(self):
        with patch.object(services, 'settings', return_value={}), patch.object(services, 'comfy_ready', AsyncMock(return_value=False)), patch.object(services, 'spawn') as spawn:
            with self.assertRaisesRegex(RuntimeError, '远程'):
                await services.ensure_comfy(None, 'http://example.com:8188')
            spawn.assert_not_called()

    async def test_waiting_napcat_is_not_launched_twice(self):
        supervisor = manager.Supervisor(lambda *args: None)
        def emit(key, value):
            if key == 'bot': supervisor.stop.set()
        supervisor.emit = emit
        with patch.object(services, 'bot_running', return_value=True), patch.object(services, 'qq_status', AsyncMock(return_value='unavailable')), patch.object(services, 'comfy_ready', AsyncMock(return_value=False)), patch.object(services, 'napcat_running', return_value=True), patch.object(services, 'recover_qq_login', AsyncMock(return_value='pending')), patch.object(services, 'spawn') as spawn:
            await supervisor.run()
            spawn.assert_not_called()

    async def test_manager_reuses_online_bot_and_qq(self):
        manager_instance = manager.Supervisor(lambda *args: None)
        def emit(key, value):
            if key == 'bot':
                manager_instance.stop.set()
        manager_instance.emit = emit
        with patch.object(services, 'bot_running', return_value=True), patch.object(services, 'qq_status', AsyncMock(return_value='online')), patch.object(services, 'comfy_ready', AsyncMock(return_value=False)), patch.object(services, 'spawn') as spawn:
            await manager_instance.run()
            spawn.assert_not_called()

    async def test_exited_napcat_can_be_started_again(self):
        supervisor = manager.Supervisor(lambda *args: None)
        supervisor.napcat_started = True  # State left by the previous implementation.
        supervisor.napcat_process = Mock(poll=lambda: 1)
        supervisor.emit = lambda key, value: supervisor.stop.set() if key == 'bot' else None
        with patch.object(services, 'bot_running', return_value=True), patch.object(services, 'qq_status', AsyncMock(return_value='unavailable')), patch.object(services, 'comfy_ready', AsyncMock(return_value=False)), patch.object(services, 'napcat_running', return_value=False), patch.object(services, 'napcat_command', return_value=(['launcher'], {})), patch.object(services, 'spawn') as spawn:
            await supervisor.run()
            spawn.assert_called_once()

    async def test_invalid_ticket_is_not_retried_every_poll(self):
        supervisor = manager.Supervisor(lambda *args: None)
        cycles = []
        def emit(key, value):
            if key == 'bot':
                cycles.append(value)
                if len(cycles) == 2:
                    supervisor.stop.set()
                    supervisor.next_login_attempt = 0
        async def no_sleep(*args):
            supervisor.next_login_attempt = 0
        supervisor.emit = emit
        with patch.object(services, 'bot_running', return_value=True), patch.object(services, 'qq_status', AsyncMock(return_value='unavailable')), patch.object(services, 'comfy_ready', AsyncMock(return_value=False)), patch.object(services, 'napcat_running', return_value=True), patch.object(services, 'recover_qq_login', AsyncMock(return_value='needs_qr')) as recover, patch.object(manager.asyncio, 'sleep', no_sleep):
            await supervisor.run()
            self.assertEqual(len(cycles), 2)
            recover.assert_awaited_once()


class LoginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        import aiohttp
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'config').mkdir()
        self.calls = []
        self.responses = {
            'auth/login': {'code': 0, 'data': {'Credential': 'test-session'}},
            'QQLogin/GetQuickLoginQQ': {'code': 0, 'data': ''},
            'QQLogin/SetQuickLoginQQ': {'code': 0, 'data': None},
            'QQLogin/CheckLoginStatus': {'code': 0, 'data': {'isLogin': False, 'loginPhase': 'waiting_qrcode'}},
            'QQLogin/GetQuickLoginList': {'code': 0, 'data': ['12345']},
            'QQLogin/SetQuickLogin': {'code': 0, 'data': None},
        }
        async def handler(request):
            name = request.match_info['name']
            self.calls.append((name, await request.json()))
            return web.json_response(self.responses[name])
        app = web.Application()
        app.router.add_post('/api/{name:.*}', handler)
        self.server = TestServer(app)
        await self.server.start_server()
        (self.root / 'config' / 'webui.json').write_text(json.dumps({'port': self.server.port, 'token': 'test-only'}))
        self.http = aiohttp.ClientSession(trust_env=False)
        self.config = {'napcat_dir': str(self.root), 'bot_qq': '12345'}

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()
        self.folder.cleanup()

    async def test_persists_account_and_reuses_saved_ticket(self):
        self.assertEqual(await services.recover_qq_login(self.http, self.config), 'pending')
        self.assertIn(('QQLogin/SetQuickLoginQQ', {'uin': '12345'}), self.calls)
        self.assertIn(('QQLogin/SetQuickLogin', {'uin': '12345'}), self.calls)

    async def test_filtered_list_does_not_prevent_a_real_quick_login_attempt(self):
        self.responses['QQLogin/GetQuickLoginList']['data'] = ['99999']
        self.assertEqual(await services.recover_qq_login(self.http, self.config), 'pending')
        self.assertIn('QQLogin/SetQuickLogin', [name for name, body in self.calls])

    async def test_rejected_ticket_requires_scan_but_transient_error_does_not(self):
        self.responses['QQLogin/GetQuickLoginList']['data'] = ['12345']
        self.responses['QQLogin/SetQuickLogin'] = {'code': -1, 'message': 'ticket expired'}
        self.assertEqual(await services.recover_qq_login(self.http, self.config), 'needs_qr')
        self.responses['QQLogin/SetQuickLogin'] = {'code': -1, 'message': 'kernel not ready'}
        self.assertEqual(await services.recover_qq_login(self.http, self.config), 'unavailable')

    async def test_known_kernel_expiry_does_not_repeat_rejected_login(self):
        for error in ('你的用户身份已失效，请你重新登录。','登录态已失效，请重新登陆。'):
            self.responses['QQLogin/CheckLoginStatus']['data']['loginError']=error
            self.assertEqual(await services.recover_qq_login(self.http, self.config),'needs_qr')
        self.assertNotIn('QQLogin/SetQuickLogin', [name for name, body in self.calls])

    async def test_reconnecting_or_logged_in_is_left_alone(self):
        for state in [{'isLogin': True}, {'loginPhase': 'reconnecting'}, {'loginPhase':'initializing'}, {'qrLoginAccepted': True}]:
            self.responses['QQLogin/CheckLoginStatus']['data'] = state
            self.assertEqual(await services.recover_qq_login(self.http, self.config), 'pending')
        self.assertNotIn('QQLogin/SetQuickLogin', [name for name, body in self.calls])
        self.assertNotIn('QQLogin/SetQuickLoginQQ', [name for name, body in self.calls])

    async def test_qr_refresh_accepts_only_new_file_on_configured_port(self):
        qr = self.root / 'cache' / 'qrcode.png'
        qr.parent.mkdir()
        qr.write_bytes(b'old')
        self.responses['QQLogin/RefreshQRcode'] = {'code': 0}
        async def write_after_ack(_):
            qr.write_bytes(b'new-qr')
        with patch.object(services.asyncio, 'sleep', write_after_ack):
            self.assertEqual(await services.refresh_napcat_qr(self.http, self.config), qr)
        with patch.object(services.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(ValueError, 'fresh QR'):
                await services.refresh_napcat_qr(self.http, self.config)


class ConfigurationTests(unittest.TestCase):
    def test_shutdown_targets_only_owned_bot_tree(self):
        import psutil
        process=Mock(pid=12345,poll=lambda:None)
        bot_child=Mock();bot_child.cmdline.return_value=['python',str(services.ROOT/'bot.py')]
        comfy_child=Mock();comfy_child.cmdline.return_value=['python','D:/ComfyUI/main.py']
        parent=Mock();parent.children.return_value=[bot_child,comfy_child]
        with patch.object(psutil,'Process',return_value=parent):
            services.terminate_owned(process)
            bot_child.terminate.assert_called_once();parent.terminate.assert_called_once()
            comfy_child.terminate.assert_not_called()
        exited=Mock(pid=12345,poll=lambda:0)
        with patch.object(services.subprocess,'run') as run:
            services.terminate_owned(exited)
            run.assert_not_called()
    def test_quick_login_argument_and_updated_relative_loader(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            napcat = root / 'NapCat'
            napcat.mkdir()
            for name in ('NapCatWinBootMain.exe', 'NapCatWinBootHook.dll', 'loadNapCat.cjs'):
                (napcat / name).touch()
            qq = root / 'QQ' / 'QQ.exe'
            qq.parent.mkdir()
            qq.touch()
            app = qq.parent / 'versions' / '9.9.99-55555' / 'resources' / 'app'
            app.mkdir(parents=True)
            (app / 'package.json').write_text(json.dumps({'version': '9.9.99', 'main': 'original.js'}))
            cmd, env = services.napcat_command(dict(napcat_dir=str(napcat), qq_exe=str(qq), bot_qq='3618079355'))
            self.assertEqual(cmd[-2:], ['-q', '3618079355'])
            package = json.loads((napcat / 'qqnt.json').read_text())
            self.assertEqual((app / package['main']).resolve(), napcat / 'loadNapCat.cjs')
            self.assertEqual(env['NAPCAT_MAIN_PATH'], str(napcat / 'napcat.mjs'))
            self.assertEqual(env['NAPCAT_QUICK_ACCOUNT'], '3618079355')

    def test_bot_file_lock_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(services, 'ROOT', Path(folder)):
            first = services.take_bot_lock()
            try:
                self.assertIsNotNone(first)
                self.assertIsNone(services.take_bot_lock())
                self.assertTrue(services.bot_running())
            finally:
                first.close()
            self.assertFalse(services.bot_running())


if __name__ == '__main__':
    unittest.main()
