"""Control the local SnowLuma installation independently of the bot."""
import msvcrt
import os
from pathlib import Path
import subprocess
import winreg

import psutil

import autostart
import services

RUN_NAME = 'WanfengSnowLuma'


def installation():
    config = services.settings()
    path = config.get('snowluma_dir')
    return Path(path).resolve() if path else None


def processes(folder):
    executable = os.path.normcase(str(folder / 'node.exe'))
    for process in psutil.process_iter(['exe']):
        if os.path.normcase(process.info['exe'] or '') == executable:
            yield process


def command():
    pythonw = Path(services.settings()['python']).with_name('pythonw.exe')
    return subprocess.list2cmdline([str(pythonw), str(Path(__file__).resolve())])


def enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == command()
    except FileNotFoundError:
        return False


def set_autostart(value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, autostart.KEY) as key:
        if value:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def status():
    folder = installation()
    return dict(available=bool(folder and (folder / 'node.exe').is_file()
                               and (folder / 'index.mjs').is_file()),
                running=bool(folder and next(processes(folder), None)),
                autostart=enabled())


def set_running(value):
    folder = installation()
    if not folder or not (folder / 'node.exe').is_file() or not (folder / 'index.mjs').is_file():
        raise FileNotFoundError('SnowLuma')
    with (folder / '.launch.lock').open('a+b') as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        current = list(processes(folder))
        if value and not current:
            env = dict(os.environ, SNOWLUMA_WEBUI_HOST='127.0.0.1', SNOWLUMA_HOOK_AUTOLOAD='1')
            services.spawn([str(folder / 'node.exe'), 'index.mjs'], str(folder), 'snowluma-startup.log', env)
        elif not value:
            # Stop the wrapper before its worker so it cannot restart the worker.
            pids = {process.pid for process in current}
            for process in sorted(current, key=lambda proc: proc.ppid() in pids):
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(current, timeout=5)
            if alive:
                raise RuntimeError('SnowLuma did not stop')


if __name__ == '__main__':
    if enabled():
        set_running(True)
