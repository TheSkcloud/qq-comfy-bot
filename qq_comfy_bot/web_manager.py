"""Loopback-only HTML dashboard; model mood data is never exposed here."""
import asyncio
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import sys
import webbrowser
from zoneinfo import ZoneInfo

from aiohttp import web
import autostart
import preferences
import image_mode
import dashboard_logs
import schedule
from life import Life, redact
from weather import DailyWeather
from plugins.onenote import read_notes, format_notes

ROOT=Path(__file__).resolve().parent


class Dashboard:
    def __init__(self,root=ROOT,token=None):
        self.root=Path(root);(self.root/'data').mkdir(parents=True,exist_ok=True)
        self.token=token or secrets.token_urlsafe(32)
        self.state={'qq':'检查中','bot':'检查中','comfy':'待命'}
        self.stop=asyncio.Event()
        self.qr_refresh_lock=asyncio.Lock()
        self.life=Life(self.root/'data')
        self.weather=DailyWeather(self.root/'data'/'weather.db')

    def emit(self,key,value):
        previous=self.state.get(key)
        self.state[key]=value
        tray=getattr(self,'tray',None)
        if tray and key=='qq':
            tray.title=('晚风 · '+str(value))[:120]
            if previous!=value and '请扫码' in str(value):
                try:tray.notify('QQ 登录已失效，请打开控制台扫码更新。','晚风 · 需要扫码')
                except Exception:logging.warning('系统通知不可用，托盘仍显示扫码状态')

    def current(self):
        return datetime.now(ZoneInfo('Asia/Shanghai'))

    def app(self):
        @web.middleware
        async def gate(request,handler):
            if request.path.startswith('/api/'):
                authorization=request.headers.get('Authorization','')
                if not secrets.compare_digest(authorization,'Bearer '+self.token):
                    raise web.HTTPUnauthorized()
                if request.method!='GET' and request.headers.get('Origin') and request.headers['Origin']!=f'http://{request.host}':
                    raise web.HTTPForbidden()
                if request.host.split(':')[0] not in ('127.0.0.1','localhost'):
                    raise web.HTTPForbidden()
            response=await handler(request)
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'"
            return response
        app=web.Application(middlewares=[gate],client_max_size=65536)
        app.router.add_get('/',self.index)
        app.router.add_get('/app.js',self.javascript)
        app.router.add_get('/api/status',self.status)
        app.router.add_get('/api/logs',self.logs)
        app.router.add_get('/api/preferences',self.get_preferences)
        app.router.add_post('/api/preferences',self.set_preferences)
        app.router.add_post('/api/autostart',self.set_autostart)
        app.router.add_get('/api/journal',self.journal)
        app.router.add_get('/api/timetable',self.timetable)
        app.router.add_post('/api/week',self.set_week)
        app.router.add_post('/api/notes',self.notes)
        app.router.add_get('/api/qrcode',self.qrcode)
        app.router.add_post('/api/refresh-qr',self.refresh_qr)
        app.router.add_post('/api/quit',self.quit)
        return app

    async def index(self,request):
        return web.FileResponse(self.root/'web'/'index.html')

    async def javascript(self,request):
        return web.FileResponse(self.root/'web'/'app.js')

    async def logs(self,request):
        stream=request.query.get('stream','all')
        if stream not in ('all','drawing','reply','login'):
            raise web.HTTPBadRequest()
        return web.json_response({'entries':await asyncio.to_thread(dashboard_logs.entries,self.root,stream)})

    async def status(self,request):
        import services
        qr=self.state.get('qr')
        qr_version=services.napcat_qr_version(Path(qr)) if qr else ''
        now=self.current()
        weather=self.weather.read(now.date().isoformat())
        # Deliberately whitelist fields: no private emotional metrics or raw note bodies.
        return web.json_response(dict(qq=self.state.get('qq'),bot=self.state.get('bot'),comfy=self.state.get('comfy'),
            hour=now.strftime('%Y.%m.%d %H时'),weather=weather or {'status':'pending','summary':'今日天气尚未同步'},
            week=schedule.week_number(now.date()),course_count=len(schedule.today(now)['courses']),
            autostart=autostart.enabled(),drawing_effective=image_mode.effective(preferences.load()),qr_version=qr_version,need_qr=bool(qr_version) and not str(self.state.get('qq')).startswith('已在线')))

    async def get_preferences(self,request):
        return web.json_response(preferences.load())

    async def set_preferences(self,request):
        try:
            preferences.save(await request.json())
        except (ValueError,TypeError):
            raise web.HTTPBadRequest(text='设置格式不正确')
        return web.json_response({'ok':True,'drawing_effective':image_mode.effective(preferences.load())})

    async def set_autostart(self,request):
        data=await request.json()
        if type(data.get('enabled')) is not bool:
            raise web.HTTPBadRequest()
        autostart.set_enabled(data['enabled'])
        return web.json_response({'ok':True})

    async def journal(self,request):
        return web.json_response({'entries':self.life.journal(30)})

    async def timetable(self,request):
        return web.json_response(dict(**schedule.today(self.current()),all_courses=schedule.load()['courses']))

    async def set_week(self,request):
        data=await request.json();week=data.get('week')
        if type(week) is not int or not 1<=week<=30:
            raise web.HTTPBadRequest()
        config=schedule.load();config.update(anchor_week=week,anchor_date=self.current().date().isoformat())
        schedule.PATH.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        return web.json_response({'ok':True})

    async def notes(self,request):
        data=await request.json();query=str(data.get('query','')).strip()[:100]
        try:
            result=await read_notes('search' if query else 'list',query)
            return web.json_response({'text':format_notes(result)})
        except Exception as exc:
            return web.json_response({'text':str(exc)[:250]},status=503)

    async def qrcode(self,request):
        import services
        path=Path(services.settings()['napcat_dir'])/'cache'/'qrcode.png'
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def refresh_qr(self,request):
        import aiohttp, services
        try:
            async with self.qr_refresh_lock:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    qr=await services.refresh_napcat_qr(session,services.settings())
                self.emit('qr',str(qr))
            return web.json_response({'ok':True})
        except (OSError, KeyError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.warning('刷新二维码失败: %s',type(exc).__name__)
            return web.json_response({'error':'未收到新的二维码，请检查 NapCat 登录状态。'},status=503)

    async def quit(self,request):
        asyncio.get_running_loop().call_later(0.5,self.stop.set)
        return web.json_response({'ok':True})


def acquire_lock():
    import msvcrt
    (ROOT/'data').mkdir(exist_ok=True)
    handle=(ROOT/'data'/'assistant.lock').open('a+b')
    if os.fstat(handle.fileno()).st_size==0:
        handle.write(b'0');handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        return handle
    except OSError:
        handle.close();return None


async def serve():
    from manager import Supervisor
    lock=acquire_lock()
    state_file=ROOT/'data'/'dashboard.json'
    if lock is None:
        if '--autostart' not in sys.argv and state_file.exists():
            webbrowser.open(json.loads(state_file.read_text(encoding='utf-8'))['url'])
        return
    controller=Dashboard()
    runner=web.AppRunner(controller.app(),access_log=None)
    await runner.setup()
    port=18088
    site=web.TCPSite(runner,'127.0.0.1',port)
    await site.start()
    url=f'http://127.0.0.1:{port}/#{controller.token}'
    state_file.write_text(json.dumps({'url':url,'pid':os.getpid()}),encoding='utf-8')
    supervisor=Supervisor(controller.emit)
    worker=asyncio.create_task(supervisor.run())
    import tray_app
    tray=None
    try:
        tray=tray_app.start(url,controller.stop,ROOT)
        controller.tray=tray
    except Exception:
        logging.exception('托盘启动失败，仍可在控制台退出')
    if '--autostart' not in sys.argv:
        webbrowser.open(url)
    try:
        await controller.stop.wait()
    finally:
        supervisor.stop.set()
        if tray:await asyncio.to_thread(tray.stop)
        try:
            await asyncio.wait_for(worker,20)
        except asyncio.TimeoutError:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        await runner.cleanup();lock.close()


def launch():
    if os.name=='nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Wanfeng.QQAssistant')
    (ROOT/'data').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,handlers=[RotatingFileHandler(ROOT/'data'/'manager.log',maxBytes=1024*1024,backupCount=2,encoding='utf-8')])
    asyncio.run(serve())
