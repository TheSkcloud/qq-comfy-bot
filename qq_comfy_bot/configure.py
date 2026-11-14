"""Local-only configuration window. No network calls or key logging."""
import json
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = Path(__file__).resolve().parent


def read_env():
    path = ROOT / '.env'
    source = path if path.exists() else ROOT / '.env.example'
    result = {}
    for line in source.read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip()
    return result


def save_config(api_key, users, bot_qq='', onebot_token=None, onebot_url=None):
    """Edit local bot settings. SnowLuma's own server settings are configured in SnowLuma."""
    api_key, users, bot_qq = api_key.strip(), users.replace('，', ',').strip(), bot_qq.strip()
    if not api_key or any(ch in api_key for ch in '\r\n\x00') or any(ch.isspace() for ch in api_key):
        raise ValueError('请输入有效的 DeepSeek API 密钥。')
    if not re.fullmatch(r'\d{5,12}(\s*,\s*\d{5,12})*', users):
        raise ValueError('请输入聊天用的 QQ 号码，多个用英文逗号隔开。')
    if bot_qq and not re.fullmatch(r'\d{5,12}', bot_qq):
        raise ValueError('机器人 QQ 号格式不正确。')
    if bot_qq and bot_qq in [x.strip() for x in users.split(',')]:
        raise ValueError('聊天 QQ 与机器人 QQ 应为不同账号。')
    values = read_env()
    old_token = values.get('ONEBOT_TOKEN') or values.get('NAPCAT_TOKEN', '')
    token = old_token if onebot_token is None else onebot_token.strip()
    if any(ch in token for ch in '\r\n\x00'):
        raise ValueError('OneBot token 不能包含换行。')
    url = (onebot_url if onebot_url is not None else
           (values.get('ONEBOT_WS_URL') or values.get('NAPCAT_WS_URL') or 'ws://127.0.0.1:3001')).strip()
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('ws', 'wss') or parsed.hostname not in ('127.0.0.1', 'localhost') or not parsed.port:
        raise ValueError('SnowLuma OneBot WebSocket 必须使用本机 ws://127.0.0.1:端口 或 wss 地址。')
    values.update(DEEPSEEK_API_KEY=api_key, ALLOWED_USERS=users,
                  ONEBOT_TOKEN=token, ONEBOT_WS_URL=url)
    values.pop('NAPCAT_WS_URL', None)
    values.pop('NAPCAT_TOKEN', None)
    (ROOT / '.env').write_text('\n'.join(f'{k}={v}' for k, v in values.items()) + '\n', encoding='utf-8')
    launcher = ROOT / 'launcher.json'
    if launcher.exists():
        config = json.loads(launcher.read_text(encoding='utf-8-sig'))
        config['qq_provider'] = 'snowluma'
        if bot_qq:
            config['bot_qq'] = bot_qq
        launcher.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    win = tk.Tk()
    win.title('QQ 虚拟助手 · 本地配置')
    win.geometry('690x455')
    win.resizable(False, False)
    box = ttk.Frame(win, padding=24)
    box.pack(fill='both', expand=True)
    ttk.Label(box, text='设置 SnowLuma OneBot 接入，密钥仅保存在这台电脑。', font=('Microsoft YaHei UI', 12)).pack(anchor='w')
    values = read_env()
    ttk.Label(box, text='DeepSeek API 密钥（粘贴 sk-…，不必发送给我）').pack(anchor='w', pady=(20, 4))
    api = tk.StringVar(value=values.get('DEEPSEEK_API_KEY', ''))
    ttk.Entry(box, textvariable=api, show='*', width=80).pack(fill='x')
    ttk.Label(box, text='你的聊天 QQ 号（平时用来给机器人发消息的账号）').pack(anchor='w', pady=(12, 4))
    users = tk.StringVar(value=values.get('ALLOWED_USERS', ''))
    ttk.Entry(box, textvariable=users).pack(fill='x')
    ttk.Label(box, text='机器人 QQ 号（可选：SnowLuma 当前登录的 QQ 账号）').pack(anchor='w', pady=(12, 4))
    botqq = tk.StringVar()
    ttk.Entry(box, textvariable=botqq).pack(fill='x')
    ttk.Label(box, text='SnowLuma OneBot token（与 SnowLuma 设置一致，可留空）').pack(anchor='w', pady=(8, 4))
    token = tk.StringVar(value=values.get('ONEBOT_TOKEN', values.get('NAPCAT_TOKEN', '')))
    ttk.Entry(box, textvariable=token, show='*').pack(fill='x')
    ttk.Label(box, text='SnowLuma OneBot WebSocket 地址').pack(anchor='w', pady=(8, 4))
    url = tk.StringVar(value=values.get('ONEBOT_WS_URL', values.get('NAPCAT_WS_URL', 'ws://127.0.0.1:3001')))
    ttk.Entry(box, textvariable=url).pack(fill='x')

    def save():
        try:
            save_config(api.get(), users.get(), botqq.get(), token.get(), url.get())
            messagebox.showinfo('已保存', '配置已保存。\n请确保 SnowLuma 已启动，并且 OneBot WebSocket 地址和 token 与软件配置一致。')
        except Exception as exc:
            messagebox.showerror('未保存', str(exc))
    ttk.Button(box, text='保存配置', command=save).pack(anchor='e')
    win.mainloop()


if __name__ == '__main__':
    main()
