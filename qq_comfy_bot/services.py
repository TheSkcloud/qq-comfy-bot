"""Local service lifecycle; no global process killing or password automation."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

import aiohttp

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'launcher.json'
_comfy_lock = asyncio.Lock()
_comfy_process = None


def take_bot_lock():
    import msvcrt
    (ROOT / 'data').mkdir(exist_ok=True)
    handle = (ROOT / 'data' / 'bot.lock').open('a+b')
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        return None


def bot_running():
    handle = take_bot_lock()
    if handle is None:
        return True
    handle.close()
    return False


def settings():
    return json.loads(CONFIG.read_text(encoding='utf-8-sig'))


def spawn(command, cwd, log_name, env=None):
    (ROOT / 'data').mkdir(exist_ok=True)
    with (ROOT / 'data' / log_name).open('ab') as output:
        return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def terminate_owned(process):
    """Stop owned bot interpreters; preserve separately useful QQ/Comfy descendants."""
    if process is None or process.poll() is not None:
        return
    if os.name=='nt':
        import psutil
        try:
            parent=psutil.Process(process.pid)
            bot_path=str(ROOT/'bot.py').casefold()
            children=parent.children(recursive=True)
            for child in reversed(children):
                try:
                    if any(str(arg).casefold()==bot_path for arg in child.cmdline()):child.terminate()
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass
            parent.terminate()
        except psutil.NoSuchProcess:pass
    else:
        process.terminate()


async def comfy_ready(http, url):
    try:
        async with http.get(url.rstrip('/') + '/system_stats', timeout=aiohttp.ClientTimeout(total=3)) as r:
            return r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def ensure_comfy(http, url):
    global _comfy_process
    if await comfy_ready(http, url):
        return
    async with _comfy_lock:
        if await comfy_ready(http, url):
            return
        config = settings()
        if not config.get('auto_start_comfy', True):
            raise RuntimeError('ComfyUI 未运行，且自动启动已关闭')
        from urllib.parse import urlparse
        if urlparse(url).hostname not in ('127.0.0.1', 'localhost'):
            raise RuntimeError('远程 ComfyUI 无法在本机自动启动，请检查服务')
        if _comfy_process is None or _comfy_process.poll() is not None:
            command = config['comfy_command']
            if not Path(command[0]).is_file() or not Path(command[2]).is_file():
                raise RuntimeError('ComfyUI 启动路径失效，请检查 launcher.json')
            _comfy_process = spawn(command, config['comfy_cwd'], 'comfy-startup.log')
        deadline = time.monotonic() + config.get('comfy_start_timeout', 180)
        while time.monotonic() < deadline:
            if await comfy_ready(http, url):
                return
            if _comfy_process.poll() is not None:
                # Another existing Desktop instance may have won the port race.
                if await comfy_ready(http, url):
                    return
                raise RuntimeError('ComfyUI 启动失败，详情见 data/comfy-startup.log')
            await asyncio.sleep(2)
        raise RuntimeError('ComfyUI 启动等待超时，详情见 data/comfy-startup.log')


def napcat_command(config):
    root = Path(config['napcat_dir'])
    qq = Path(config['qq_exe'])
    for p in (qq, root / 'NapCatWinBootMain.exe', root / 'NapCatWinBootHook.dll', root / 'loadNapCat.cjs'):
        if not p.is_file():
            raise ValueError(f'启动文件不存在：{p}')
    account = str(config.get('bot_qq', ''))
    if not account.isdigit():
        raise ValueError('请先在 launcher.json 配置机器人 QQ 号码')
    # Recompute relative entry from installed QQ version, in case QQ was updated.
    versions = qq.parent / 'versions'
    candidates = [p for p in versions.glob('*/resources/app/package.json') if p.is_file()]
    if not candidates:
        raise ValueError('未找到 QQ 的 package.json')
    source = max(candidates, key=lambda p: tuple(int(x) for x in __import__('re').findall(r'\d+', p.parents[2].name)))
    package = json.loads(source.read_text(encoding='utf-8-sig'))
    package['main'] = os.path.relpath(root / 'loadNapCat.cjs', source.parent).replace('\\', '/')
    (root / 'qqnt.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
    env = os.environ.copy()
    # Inherited by NapCat's restarted workers; argv -q alone is lost on resume.
    env['NAPCAT_QUICK_ACCOUNT'] = account
    env['NAPCAT_WORKDIR'] = str(root.resolve())
    if config.get('qq_data_dir'):
        data_dir=Path(config['qq_data_dir']).resolve()
        if not data_dir.is_dir():
            raise ValueError('QQ 数据目录不存在；已停止启动，避免新建设备身份和登录记录')
        env['NAPCAT_QQ_DATA_DIR']=str(data_dir)
    for key, name in [('NAPCAT_PATCH_PACKAGE', 'qqnt.json'), ('NAPCAT_LOAD_PATH', 'loadNapCat.cjs'),
                      ('NAPCAT_MAIN_PATH', 'napcat.mjs'), ('NAPCAT_INJECT_PATH', 'NapCatWinBootHook.dll')]:
        env[key] = str(root / name)
    return [str(root / 'NapCatWinBootMain.exe'), str(qq), str(root / 'NapCatWinBootHook.dll'), '-q', account], env


async def qq_status(http, url, token, expected_account=None):
    """Read-only OneBot probe: does not send QQ messages."""
    try:
        async with http.ws_connect(url, headers={'Authorization': 'Bearer ' + token}) as ws:
            for action in ('get_status', 'get_login_info'):
                await ws.send_json(dict(action=action, params={}, echo=action))
            result = {}
            deadline = time.monotonic() + 5
            while len(result) < 2 and time.monotonic() < deadline:
                data = await ws.receive_json(timeout=3)
                if data.get('echo') in ('get_status', 'get_login_info'):
                    result[data['echo']] = data
            status = result.get('get_status', {})
            login = result.get('get_login_info', {})
            if expected_account and str(login.get('data', {}).get('user_id', '')) != str(expected_account):
                return 'wrong_account'
            return 'online' if status.get('data', {}).get('online') else 'offline'
    except aiohttp.WSServerHandshakeError as exc:
        return 'token_error' if exc.status in (401, 403) else 'unavailable'
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        return 'unavailable'


def napcat_running(config):
    """Detect only our own NapCat launcher; fail closed on process-access errors."""
    import psutil
    expected = (Path(config['napcat_dir']) / 'NapCatWinBootMain.exe').resolve()
    for proc in psutil.process_iter(['name','exe','cmdline']):
        try:
            if (proc.info.get('name') or '').lower() != 'napcatwinbootmain.exe':
                continue
            exe = proc.info.get('exe')
            if not exe:
                return True
            if Path(exe).resolve() == expected:
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            return True
    return False


async def recover_qq_login(http, config):
    """Reuse QQ's saved ticket through local NapCat; never use passwords or log tokens."""
    import hashlib
    try:
        web = json.loads((Path(config['napcat_dir']) / 'config' / 'webui.json').read_text(encoding='utf-8-sig'))
        if web.get('enable2FA'):
            return 'manual_auth'
        port = int(web.get('port', 6099))
        base = f'http://127.0.0.1:{port}/api/'
        timeout = aiohttp.ClientTimeout(total=12)
        async def call(path, body, headers=None):
            async with http.post(base + path, json=body, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                return await response.json()
        auth = await call('auth/login', {'hash': hashlib.sha256((web['token'] + '.napcat').encode()).hexdigest()})
        credential = (auth.get('data') or {}).get('Credential')
        if not credential:
            return 'manual_auth'
        headers = {'Authorization': 'Bearer ' + credential}
        account = str(config['bot_qq'])
        saved = await call('QQLogin/GetQuickLoginQQ', {}, headers)
        if saved.get('code') != 0:
            return 'unavailable'
        if saved.get('data') != account:
            saved = await call('QQLogin/SetQuickLoginQQ', {'uin': account}, headers)
            if saved.get('code') != 0:
                return 'unavailable'
        status = await call('QQLogin/CheckLoginStatus', {}, headers)
        if status.get('code') != 0:
            return 'unavailable'
        state = status.get('data') or {}
        # Let the kernel finish reconnecting and never switch an already logged-in account.
        if state.get('isLogin') or state.get('loginPhase') in ('reconnecting','qrcode_scanned','initializing') or state.get('qrLoginAccepted'):
            return 'pending'
        def requires_scan(message):
            return any(word in str(message).lower() for word in ('身份已失效','登录态已失效','登录状态已失效','凭据过期','ticket expired','ticket invalid','重新登录','重新登陆','重新扫码','没有登录记录','未找到该 qq 历史'))
        # Read the actual kernel failure first. The quick-login list is a filtered,
        # cached UI snapshot, not authoritative evidence that no valid ticket exists.
        if requires_scan(state.get('loginError','')):
            return 'needs_qr'
        result = await call('QQLogin/SetQuickLogin', {'uin': account}, headers)
        if result.get('code') == 0:
            return 'pending'
        # Network/kernel initialization failures must not permanently disable recovery.
        message=result.get('message') or result.get('msg','')
        return 'needs_qr' if requires_scan(message) else 'unavailable'
    except (OSError, KeyError, ValueError, TypeError, aiohttp.ClientError, asyncio.TimeoutError):
        return 'unavailable'
