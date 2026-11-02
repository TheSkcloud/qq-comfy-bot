"""Local-only configuration window. No network calls or key logging."""
import json
from pathlib import Path
import re
import secrets
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


def save_config(api_key, users, bot_qq=''):
    api_key, users, bot_qq = api_key.strip(), users.replace('，', ',').strip(), bot_qq.strip()
    if not api_key or any(ch in api_key for ch in '\r\n\x00') or any(ch.isspace() for ch in api_key):
        raise ValueError('请输入有效的 DeepSeek API 密钥，不要包含空格或换行。')
    if not re.fullmatch(r'\d{5,12}(\s*,\s*\d{5,12})*', users):
        raise ValueError('请输入你聊天用的 QQ 号码；多个号码用英文逗号隔开。')
    if bot_qq and not re.fullmatch(r'\d{5,12}', bot_qq):
        raise ValueError('机器人 QQ 号格式不正确；不知道时可以留空。')
    if bot_qq and bot_qq in [x.strip() for x in users.split(',')]:
        raise ValueError('聊天 QQ 与机器人 QQ 应为不同账号，机器人不会回应自己的消息。')
    values = read_env()
    token = values.get('NAPCAT_TOKEN', '')
    if not token or '替换' in token:
        token = secrets.token_urlsafe(32)
    values.update(DEEPSEEK_API_KEY=api_key, ALLOWED_USERS=users, NAPCAT_TOKEN=token,
                  NAPCAT_WS_URL='ws://127.0.0.1:3001')
    config_dir = ROOT.parent / 'NapCat' / 'config'
    if config_dir.is_dir():
        paths = [config_dir / 'onebot11.json']
        if bot_qq:
            paths.append(config_dir / f'onebot11_{bot_qq}.json')
        for path in paths:
            config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            network = config.setdefault('network', {})
            servers = network.setdefault('websocketServers', [])
            server = next((s for s in servers if s.get('name') == 'qq-virtual-assistant'), None)
            if server is None:
                server = {'name': 'qq-virtual-assistant'}
                servers.append(server)
            server.update(enable=True, host='127.0.0.1', port=3001, token=token,
                          messagePostFormat='array', reportSelfMessage=False,
                          enableForcePushEvent=True, debug=False, heartInterval=30000)
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / '.env').write_text('\n'.join(f'{k}={v}' for k, v in values.items()) + '\n', encoding='utf-8')
    launcher = ROOT / 'launcher.json'
    if bot_qq and launcher.exists():
        config = json.loads(launcher.read_text(encoding='utf-8-sig'))
        config['bot_qq'] = bot_qq
        launcher.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    win = tk.Tk()
    win.title('QQ 虚拟助手 · 本地配置')
    win.geometry('690x380')
    win.resizable(False, False)
    box = ttk.Frame(win, padding=24)
    box.pack(fill='both', expand=True)
    ttk.Label(box, text='只需填写下面两项，密钥仅保存到这台电脑。', font=('Microsoft YaHei UI', 12)).pack(anchor='w')
    values = read_env()
    ttk.Label(box, text='DeepSeek API 密钥（粘贴 sk-…，不必发送给我）').pack(anchor='w', pady=(20, 4))
    api = tk.StringVar(value=values.get('DEEPSEEK_API_KEY', ''))
    ttk.Entry(box, textvariable=api, show='*', width=80).pack(fill='x')
    ttk.Label(box, text='你的聊天 QQ 号（平时用来给机器人发消息的账号）').pack(anchor='w', pady=(12, 4))
    users = tk.StringVar(value=values.get('ALLOWED_USERS', ''))
    ttk.Entry(box, textvariable=users).pack(fill='x')
    ttk.Label(box, text='机器人 QQ 号（可选：NapCat 扫码登录的另一个账号）').pack(anchor='w', pady=(12, 4))
    botqq = tk.StringVar()
    ttk.Entry(box, textvariable=botqq).pack(fill='x')
    ttk.Label(box, text='NapCat token 已自动生成，不需要你手动复制。保存后重启 NapCat 和 bot。').pack(anchor='w', pady=12)

    def save():
        try:
            save_config(api.get(), users.get(), botqq.get())
            messagebox.showinfo('已保存', '配置已保存。\n先启动 NapCat 并扫码登录机器人 QQ，再启动虚拟助手。')
        except Exception as exc:
            messagebox.showerror('未保存', str(exc))
    ttk.Button(box, text='保存配置', command=save).pack(anchor='e')
    win.mainloop()


if __name__ == '__main__':
    main()
