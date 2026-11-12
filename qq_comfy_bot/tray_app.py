"""Windows notification-area entry; callbacks dispatch to the dashboard event loop."""
import asyncio
import logging
import os
import webbrowser
from pathlib import Path


def start(url, stop, root):
    if os.name!='nt':return None
    import pystray
    from PIL import Image
    loop=asyncio.get_running_loop()
    def open_dashboard(icon=None,item=None):webbrowser.open(url)
    def open_logs(icon=None,item=None):os.startfile(str(Path(root)/'data'))
    def quit_app(icon=None,item=None):
        loop.call_soon_threadsafe(stop.set)
    image=Image.open(Path(root)/'assets'/'wanfeng.ico').copy()
    icon=pystray.Icon('WanfengQQAssistant',image,'晚风 · QQ 助手',pystray.Menu(
        pystray.MenuItem('打开控制台',open_dashboard,default=True),
        pystray.MenuItem('查看日志',open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出助手',quit_app)))
    icon.run_detached()
    logging.info('Windows 托盘已启动；右键图标可退出助手')
    return icon
