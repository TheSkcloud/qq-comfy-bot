"""One visible control window, one supervised bot, reuse persistent QQ login."""
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import aiohttp
from dotenv import load_dotenv
import services
import login_checkpoint

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


class Supervisor:
    def __init__(self, emit):
        self.emit = emit
        self.config = services.settings()
        self.stop = threading.Event()
        self.bot_process = None
        self.napcat_process = None
        self.next_napcat_attempt = 0
        self.next_bot_attempt = 0
        self.qr_started = 0
        self.last_online = 0
        self.next_login_attempt = 0
        self.login_needs_qr = False
        self.last_qq_state = None
        login_checkpoint.record(ROOT,self.config,'supervisor_start','starting')

    def start_bot(self):
        self.bot_process = services.spawn([self.config['python'], str(ROOT / 'bot.py')],
                                          str(ROOT), 'bot-console.log')

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as http:
            while not self.stop.is_set():
                try:
                    if not services.bot_running() and (not self.bot_process or self.bot_process.poll() is not None):
                        if time.monotonic() >= self.next_bot_attempt:
                            self.start_bot()
                            self.next_bot_attempt = time.monotonic() + 30
                    state = await services.qq_status(http, os.environ.get('NAPCAT_WS_URL', 'ws://127.0.0.1:3001'),
                                                     os.environ.get('NAPCAT_TOKEN', ''), self.config['bot_qq'])
                    if state!=self.last_qq_state:
                        login_checkpoint.record(ROOT,self.config,'connection_change',state,previous_state=self.last_qq_state,
                            worker_exit_code=self.napcat_process.poll() if self.napcat_process else None)
                        self.last_qq_state=state
                    if state == 'online':
                        self.emit('qq', '已在线')
                        self.qr_started = 0
                        self.login_needs_qr = False
                        self.next_login_attempt = time.monotonic() + 30
                        self.emit('qr', '')
                        self.last_online = time.time()
                    elif state in ('wrong_account', 'token_error'):
                        self.emit('qq', '连接账号不符' if state == 'wrong_account' else 'token 不匹配，请检查配置')
                    else:
                        # Never kill QQ or launch another instance every polling cycle.
                        if time.monotonic() >= self.next_napcat_attempt and not await asyncio.to_thread(services.napcat_running, self.config):
                            command, env = services.napcat_command(self.config)
                            self.napcat_process = services.spawn(command, self.config['napcat_dir'], 'napcat-startup.log', env)
                            self.next_napcat_attempt = time.monotonic() + 120
                            self.qr_started = time.time()
                            self.next_login_attempt = time.monotonic() + 30
                            self.emit('qq', '正在使用已保存账号快捷登录…')
                        else:
                            if not self.login_needs_qr and time.monotonic() >= self.next_login_attempt:
                                self.next_login_attempt = time.monotonic() + 120
                                diagnostics={}
                                recovery = await services.recover_qq_login(http, self.config,diagnostics)
                                self.login_needs_qr = recovery == 'needs_qr'
                                login_checkpoint.record(ROOT,self.config,'login_recovery',recovery,**diagnostics)
                                logging.info('QQ 登录恢复：%s', recovery)
                            self.emit('qq', 'QQ 登录凭据已失效，请扫码一次更新登录' if self.login_needs_qr
                                      else '正在等待网络恢复并复用已保存登录…')
                        qr = Path(self.config['napcat_dir']) / 'cache' / 'qrcode.png'
                        if qr.is_file() and qr.stat().st_mtime >= max(self.qr_started, self.last_online):
                            self.emit('qr', str(qr))
                    comfy = await services.comfy_ready(http, os.getenv('COMFYUI_URL', 'http://127.0.0.1:8188'))
                    self.emit('comfy', '已运行' if comfy else '待命 · 需要画图时自动启动')
                    self.emit('bot', '运行中' if services.bot_running() else '启动失败，查看日志')
                except Exception as exc:
                    logging.error('管理器错误：%s', type(exc).__name__)
                    self.emit('qq', str(exc)[:180])
                for _ in range(10):
                    if self.stop.is_set():
                        break
                    await asyncio.sleep(0.5)
        if self.bot_process and self.bot_process.poll() is None:
            await asyncio.to_thread(services.terminate_owned,self.bot_process)
            try:
                await asyncio.to_thread(self.bot_process.wait, 8)
            except Exception:
                pass
        # Leave QQ and ComfyUI alone: they may also be used by the user.


def main():
    import msvcrt
    import autostart
    import preferences
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from plugins.onenote import read_notes, format_notes
    (ROOT / 'data').mkdir(exist_ok=True)
    lock = (ROOT / 'data' / 'assistant.lock').open('a+b')
    if os.fstat(lock.fileno()).st_size == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        messagebox.showinfo('助手已运行', '助手已经在运行，请查看任务栏中的“晚风 · QQ 助手”。')
        return
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        ROOT / 'data' / 'manager.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')])
    ui = tk.Tk()
    ui.title('晚风 · QQ 助手')
    ui.geometry('1000x790')
    ui.minsize(880, 700)
    ui.configure(bg='#0c1220')
    ui.option_add('*Font', ('Microsoft YaHei UI', 10))
    style = ttk.Style(ui)
    style.theme_use('clam')
    style.configure('TCheckbutton', background='#172137', foreground='#e5ecfa', padding=8)
    style.map('TCheckbutton', background=[('active', '#22304a')])
    style.configure('TSpinbox', fieldbackground='#22304a', foreground='#e5ecfa', padding=5)
    sidebar = tk.Frame(ui, bg='#111a2b', width=185)
    sidebar.pack(side='left', fill='y')
    sidebar.pack_propagate(False)
    tk.Label(sidebar, text='晚风', font=('Microsoft YaHei UI', 25, 'bold'), fg='#dfe9ff', bg='#111a2b').pack(anchor='w', padx=25, pady=(30, 0))
    tk.Label(sidebar, text='你的 QQ 陪伴助手', fg='#92a5c4', bg='#111a2b').pack(anchor='w', padx=25, pady=(6, 30))
    canvas = tk.Frame(ui, bg='#0c1220')
    canvas.pack(side='left', fill='both', expand=True, padx=28, pady=25)
    clock = tk.StringVar()
    tk.Label(canvas, textvariable=clock, font=('Microsoft YaHei UI', 12), fg='#9eb8e9', bg='#0c1220').pack(anchor='e')
    title = tk.StringVar(value='一切就绪，从一句话开始')
    tk.Label(canvas, textvariable=title, font=('Microsoft YaHei UI', 22, 'bold'), fg='#f2f6ff', bg='#0c1220').pack(anchor='w', pady=(12, 8))
    tk.Label(canvas, text='聊天、照片、随手表情和你的笔记，都在这里。', fg='#95a6c0', bg='#0c1220').pack(anchor='w', pady=(0, 22))
    stack = tk.Frame(canvas, bg='#0c1220')
    stack.pack(fill='both', expand=True)
    stack.grid_rowconfigure(0, weight=1)
    stack.grid_columnconfigure(0, weight=1)
    pages = {}
    for name in ('总览', '个性化', 'OneNote'):
        page = tk.Frame(stack, bg='#0c1220')
        page.grid(row=0, column=0, sticky='nsew')
        pages[name] = page
    def nav(name):
        pages[name].tkraise()
        title.set({'总览':'一切就绪，从一句话开始', '个性化':'按你的习惯来', 'OneNote':'把想法接进聊天'}[name])
    def button(parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg='#779eff' if primary else '#22304a',
                         fg='#091326' if primary else '#e5ecfa', activebackground='#99b7ff', activeforeground='#091326',
                         relief='flat', bd=0, padx=17, pady=10, cursor='hand2')
    for name in pages:
        button(sidebar, name, lambda n=name: nav(n)).pack(fill='x', padx=16, pady=6)
    button(sidebar, '日志文件夹', lambda: os.startfile(str(ROOT / 'data'))).pack(fill='x', padx=16, pady=6)
    tk.Label(sidebar, text='关闭窗口会最小化\n聊天记录保存在本机', fg='#7f92b3', bg='#111a2b', justify='left').pack(side='bottom', anchor='w', padx=20, pady=25)
    labels = {}
    for key, heading in [('qq','QQ 连接'),('bot','对话助手'),('comfy','绘图引擎')]:
        card = tk.Frame(pages['总览'], bg='#172137', padx=20, pady=15)
        card.pack(fill='x', pady=(0, 12))
        tk.Label(card, text=heading, fg='#91a4c3', bg='#172137').pack(anchor='w')
        labels[key] = tk.StringVar(value='检查中…')
        tk.Label(card, textvariable=labels[key], fg='#e5edff', bg='#172137', font=('Microsoft YaHei UI', 12, 'bold'), wraplength=610, justify='left').pack(anchor='w', pady=(7,0))
    qr_label = tk.Label(pages['总览'], text='已有登录有效时无需扫码。\n绘图按需启动，等待期间可以继续聊天。', fg='#9bacca', bg='#0c1220', justify='center')
    qr_label.pack(pady=5)
    config = preferences.load()
    panel = tk.Frame(pages['个性化'], bg='#172137', padx=20, pady=18)
    panel.pack(fill='x')
    auto = tk.BooleanVar(value=autostart.enabled())
    reactions = tk.BooleanVar(value=config['reactions'])
    image_chat = tk.BooleanVar(value=config['image_chat'])
    interval = tk.IntVar(value=config['image_chat_interval'])
    feedback = tk.StringVar(value='更改立即保存；不需要重启聊天助手。')
    def save_prefs():
        try:
            preferences.save(dict(reactions=reactions.get(), image_chat=image_chat.get(), image_chat_interval=interval.get(), image_chat_max=4))
            feedback.set('已保存，下一轮对话立即生效。')
        except Exception as exc:
            feedback.set(str(exc))
    def toggle_start():
        try:
            autostart.set_enabled(auto.get())
            feedback.set('已开启登录桌面后自动启动。' if auto.get() else '已关闭开机自启动。')
        except Exception as exc:
            feedback.set(str(exc))
    ttk.Checkbutton(panel, text='登录 Windows 后自动启动（最小化）', variable=auto, command=toggle_start).pack(anchor='w')
    ttk.Checkbutton(panel, text='按语境发送小表情图片（有频率限制）', variable=reactions, command=save_prefs).pack(anchor='w')
    ttk.Checkbutton(panel, text='生图等待期间继续聊天，用户插话后停止预设短句', variable=image_chat, command=save_prefs).pack(anchor='w')
    row = tk.Frame(panel, bg='#172137')
    row.pack(fill='x', pady=12)
    tk.Label(row, text='追加短句间隔（秒）', fg='#becbe1', bg='#172137').pack(side='left')
    ttk.Spinbox(row, from_=5, to=30, textvariable=interval, width=5, command=save_prefs).pack(side='left', padx=12)
    button(row, '保存', save_prefs).pack(side='left')
    tk.Label(panel, textvariable=feedback, fg='#9eb8e9', bg='#172137', wraplength=580).pack(anchor='w', pady=8)
    tk.Label(pages['个性化'], text='人设与口吻可在 persona.md 中编辑；时间由程序实时注入。', fg='#92a5c4', bg='#0c1220', wraplength=600).pack(anchor='w', pady=(20,10))
    button(pages['个性化'], '编辑人设', lambda: os.startfile(str(ROOT/'persona.md'))).pack(anchor='w')
    note_status = tk.StringVar(value='只读连接本机 OneNote，读取已同步内容。\n可在 QQ 私聊发 /笔记 或 /笔记 关键词。')
    tk.Label(pages['OneNote'], textvariable=note_status, fg='#b4c7e8', bg='#0c1220', justify='left', wraplength=620).pack(anchor='w', pady=(0,14))
    notes_box = tk.Text(pages['OneNote'], bg='#172137', fg='#dce8fd', relief='flat', padx=18, pady=18, wrap='word', height=15, insertbackground='#ffffff')
    notes_box.pack(fill='both', expand=True, pady=12)
    messages = queue.Queue()
    def fetch_notes():
        note_status.set('正在读取本机已同步的页面目录…')
        def work():
            try:
                text = format_notes(asyncio.run(read_notes()))
            except Exception as exc:
                text = str(exc)
            messages.put(('notes', text))
        threading.Thread(target=work, daemon=True).start()
    button(pages['OneNote'], '读取 OneNote 目录', fetch_notes, True).pack(anchor='w')
    qr_image, qr_version = [None], [None]
    supervisor = Supervisor(lambda key, value: messages.put((key, value)))
    worker = threading.Thread(target=lambda: asyncio.run(supervisor.run()), daemon=True)
    worker.start()
    def refresh():
        while True:
            try:
                key, value = messages.get_nowait()
            except queue.Empty:
                break
            if key in labels:
                labels[key].set(value)
                if key == 'qq' and value.startswith('已在线'):
                    qr_label.configure(image='', text='QQ 已在线，直接在聊天里找我吧。')
            elif key == 'notes':
                notes_box.delete('1.0','end')
                notes_box.insert('1.0', value)
                note_status.set('已读取本机同步内容；不会编辑或删除笔记。')
            elif key == 'qr':
                if not value:
                    qr_version[0] = None
                    continue
                path = Path(value)
                stamp = path.stat().st_mtime_ns
                if stamp != qr_version[0]:
                    try:
                        image = tk.PhotoImage(file=value)
                        factor = max(1, (image.width()+209)//210)
                        qr_image[0] = image.subsample(factor)
                        qr_label.configure(image=qr_image[0], text='')
                        qr_version[0] = stamp
                        nav('总览')
                        ui.deiconify()
                    except tk.TclError:
                        pass
        ui.after(500, refresh)
    def tick():
        current = datetime.now(ZoneInfo(os.getenv('TIMEZONE','Asia/Shanghai')))
        clock.set(current.strftime('%Y.%m.%d  %H:%M:%S')+'  北京时间')
        ui.after(1000,tick)
    def quit_app():
        supervisor.stop.set()
        labels['bot'].set('正在退出…')
        def finish():
            if worker.is_alive():
                ui.after(300,finish)
            else:
                ui.destroy()
        finish()
    footer = tk.Frame(canvas, bg='#0c1220')
    footer.pack(fill='x', pady=(16,0))
    button(footer,'最小化',ui.iconify).pack(side='left')
    button(footer,'退出助手',quit_app).pack(side='right')
    ui.protocol('WM_DELETE_WINDOW', ui.iconify)
    nav('总览')
    refresh()
    tick()
    if '--autostart' in sys.argv:
        ui.iconify()
    ui.mainloop()
    lock.close()


if __name__ == '__main__':
    try:
        if '--diagnose' in sys.argv:
            async def diagnose():
                config = services.settings()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as http:
                    status = await services.qq_status(http, os.getenv('NAPCAT_WS_URL', 'ws://127.0.0.1:3001'), os.getenv('NAPCAT_TOKEN', ''), config['bot_qq'])
                    comfy = await services.comfy_ready(http, os.getenv('COMFYUI_URL', 'http://127.0.0.1:8188'))
                    print(json.dumps(dict(qq=status, comfy_ready=comfy, bot_running=services.bot_running(), auto_start_comfy=config.get('auto_start_comfy', True))))
            asyncio.run(diagnose())
        else:
            from web_manager import launch
            launch()
    except Exception as exc:
        messagebox.showerror('启动失败', f'{type(exc).__name__}: {exc}')
