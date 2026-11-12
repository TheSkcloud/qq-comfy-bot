import asyncio
import json
import os
from pathlib import Path


async def read_notes(action='list', query=''):
    script = Path(__file__).with_suffix('.ps1').read_text(encoding='utf-8-sig')
    # User content is JSON on stdin, never interpolated into executable PowerShell.
    proc = await asyncio.create_subprocess_exec(
        'powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-Command', script,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0))
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(json.dumps(dict(action=action, query=query), ensure_ascii=True).encode('ascii')), 40)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError('OneNote 读取超时，请先确认桌面版已登录并完成同步') from None
    try:
        result = json.loads(stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise RuntimeError('本地 OneNote 接口不可用，请打开桌面版 OneNote 完成登录') from None
    if result.get('error'):
        raise RuntimeError('OneNote 只读插件：' + result['error'][:200])
    return result


def format_notes(data):
    pages = data.get('pages', [])
    if not pages:
        return '在本次检索范围内没有找到匹配笔记。正文搜索最多检查最近 10 页。'
    lines = ['OneNote · 本机同步内容（只读）']
    for page in pages:
        lines.append('\n' + page.get('title', '无标题'))
        if page.get('text'):
            lines.append(page['text'][:1300])
        if page.get('truncated'):
            lines.append('（页面较长，仅展示节选）')
    if 'scanned' in data:
        lines.append(f'\n本次范围：{data["scope"]}，最多检查 {data["scanned"]} 页。')
    else:
        lines.append('\n用 /笔记 关键词 检索；不会修改笔记。')
    return '\n'.join(lines)[:3500]
