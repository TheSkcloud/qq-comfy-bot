"""Explicit per-user Windows sign-in startup, configurable in the dashboard."""
from pathlib import Path
import subprocess
import sys
import winreg

KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
NAME = 'QQVirtualAssistant'


def command():
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    return subprocess.list2cmdline([str(pythonw), str(Path(__file__).with_name('manager.py')), '--autostart'])


def enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value, _ = winreg.QueryValueEx(key, NAME)
            return value == command()
    except FileNotFoundError:
        return False


def set_enabled(value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        if value:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass


if __name__ == '__main__':
    set_enabled('--disable' not in sys.argv)
    print('Autostart enabled:' if enabled() else 'Autostart disabled:', enabled())
