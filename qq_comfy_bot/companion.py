"""Character-local continuity and conversation direction, without model-assigned romance scores."""
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import re
import unicodedata


def character_key(character):
    card = load_card(character)
    if card.get('id'):
        return 'card:' + card['id']
    normalized = ' '.join(unicodedata.normalize('NFKC', character).casefold().split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]


def load_card(character):
    try:
        cards = json.loads(Path(__file__).with_name('character_card.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(cards, list):
        return {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get('aliases'), list):
            continue
        aliases = [a for a in card['aliases'] if isinstance(a, str) and a.strip()]
        if not any(alias.casefold() in character.casefold() for alias in aliases):
            continue
        clean = {k: card[k][:1600] for k in ('core','voice','visual_identity') if isinstance(card.get(k), str)}
        if isinstance(card.get('id'), str) and re.fullmatch(r'[a-z0-9_-]{1,48}', card['id']):
            clean['id'] = card['id']
        for key in ('preferences','values','mannerisms'):
            clean[key] = [v[:160] for v in card.get(key, []) if isinstance(v, str)][:5] if isinstance(card.get(key), list) else []
        clean['lore'] = [entry for entry in card.get('lore', []) if isinstance(entry, dict)
                         and isinstance(entry.get('keys'), list) and isinstance(entry.get('content'), str)] if isinstance(card.get('lore'), list) else []
        return clean
    return {}


def character_context(character, message):
    card = load_card(character)
    if not card:
        return '角色身份以当前设定为准；保留自己的小偏好与生活，不继承其他角色的职业、称呼或经历。'
    parts = ['本地角色演绎设定（非官方考据；用户当前设定优先）：', card.get('core',''), card.get('voice','')]
    for field, label in [('preferences','稳定的小偏好'), ('values','待人处事'), ('mannerisms','偶尔出现的习惯')]:
        if card.get(field) and (field=='values' or any(k in message for k in ('喜欢','习惯','平时','你会','爱好','音乐','画','生活'))):
            parts.append(label+'：'+'；'.join(card[field][:2])+'。按话题自然表现，不逐条念设定。')
    for entry in card.get('lore', []):
        if any(isinstance(key,str) and key and key in message for key in entry['keys']):
            parts.append(entry['content'][:500])
            if len(parts) >= 9:
                break
    return '\n'.join(parts)


def conversation_direction(message, history):
    recent = [item.get('content', '') for item in history[-8:] if item.get('role') == 'assistant']
    parts = ['本轮先回应用户具体内容，再选择一个自然延伸；无需每条提自己的日程。']
    if not any(word in message for word in ('你在', '你呢', '你今天', '你刚才', '在忙什么', '照片', '拍照', '看看你', '插画')):
        parts.append('用户这轮没有问你的活动：优先停留在用户的话题，通常不补一句“我这边正在……”或把日程硬接进来。')
    if sum('？' in item or '?' in item for item in recent) >= 2:
        parts.append('最近问句较多，这轮优先陈述、分享一个具体反应，不再连环提问。')
    if any(word in message for word in ('学习', '作业', '忙', '上课', '开会', '晚点聊', '先走', '睡了')):
        parts.append('用户可能有现实安排：简短接住，不挽留、不加新问题；除非明确求助，不布置建议。')
    if any(word in message for word in ('累', '难过', '烦', '委屈', '失落', '不开心')):
        parts.append('先具体回应处境，少说泛泛的安慰；没有求建议时不列解决清单，不忽然撒娇或转去拍照。')
    if any(word in message for word in ('报错', '代码', '怎么做', '帮我', '为什么')):
        parts.append('实质求助先准确回答；角色口吻不能妨碍有用信息，不把技术问题改成暧昧对话。')
    return '\n'.join(parts)


class Companion:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS visits (user TEXT, character TEXT, day TEXT, last_at TEXT, PRIMARY KEY(user,character,day))')
            db.execute('CREATE TABLE IF NOT EXISTS anniversaries (user TEXT, name TEXT, day TEXT, PRIMARY KEY(user,name))')

    def touch(self, user, character, current):
        with closing(sqlite3.connect(self.path)) as db, db:
            self._migrate_identity(db, str(user), character)
            db.execute('INSERT OR REPLACE INTO visits VALUES (?,?,?,?)',
                       (str(user), character_key(character), current.date().isoformat(), current.isoformat()))

    def context(self, user, character, current, mode='slow'):
        with closing(sqlite3.connect(self.path)) as db, db:
            self._migrate_identity(db, str(user), character)
            days, latest = db.execute('SELECT COUNT(*),MAX(last_at) FROM visits WHERE user=? AND character=?',
                                      (str(user), character_key(character))).fetchone()
            dates = db.execute('SELECT name,day FROM anniversaries WHERE user=? ORDER BY name', (str(user),)).fetchall()
        familiarity = ('当前关系阶段：初识、有距离。了解仍有限。冷淡克制，短句平直，不主动关心饮食睡眠，不哄人，不主动安慰或接受恋人式亲近；仍正常接话、认真答问题。沿用已有事实，不突然自我介绍。' if days < 7 else
            '当前关系阶段：开始熟悉。已有一些熟悉感，可以多说一点自己的意见、偶尔接玩笑；仍不频繁关心或主动暧昧，不装作多年恋人。' if days < 21 else
            '当前关系阶段：相互信任。已有持续相处的熟悉感，可主动接续真实共同话题、分享小事，偶尔表达克制的关心；不默认交往。' if days < 45 else
            '当前关系阶段：亲近但独立。此阶段已越过初识的疏离，不再以还不熟、太突然来挡回普通好感；收到想念可坦率承认开心或轻轻回应想念，不只故作冷淡。对低落可表达站在对方这边的态度，不先用二选一问题盘问原因，也不讽刺对方需要提醒。愿意主动分享、表达少量关心，仍不事事照顾、不反复问候。共同经历须有依据，确立恋人关系仍需明确选择。')
        relationship = {
            'slow': '相处方式：从有距离的初识慢慢亲近。阶段决定亲近上限，不默认热情、安慰或暧昧；实际相处逐渐积累，不靠刷消息或索取关心直接解锁。',
            'friends': '相处方式：朋友陪伴，不主动营造恋爱关系。',
            'partner': '相处方式：用户明确选择的虚拟恋人模式；自然亲近，仍尊重用户现实安排和个人空间。',
        }.get(mode, '')
        if mode=='partner':
            familiarity='用户已明确选择恋人模式，可自然表达亲近，但仍独立、克制，不默认承担照料用户生活的职责。'
        parts = [relationship, familiarity,
                 '角色有自己的生活和不同意见；用户离线时不扣关系、不追责、不说一直等你；不能声称监视到用户的活动。']
        if latest:
            gap = max(0, (current - datetime.fromisoformat(latest)).total_seconds() / 3600)
            if gap >= 12:
                parts.append('隔了一段时间才再聊：可自然接上，旧话题未必仍在进行，不追问失联原因。')
        for name, day in dates:
            # Month-day values are validated on explicit entry; no guessed holidays.
            if current.strftime('%m-%d') == day:
                parts.append(f'用户明确登记的今天纪念日：{name}。相关时轻轻提一次，不编造庆祝经历。')
        return '\n'.join(parts)

    def _migrate_identity(self, db, user, character):
        legacy = hashlib.sha256(character.encode('utf-8')).hexdigest()[:20]
        key = character_key(character)
        if legacy != key:
            db.execute('INSERT INTO visits SELECT user,?,day,last_at FROM visits WHERE user=? AND character=? '
                       'ON CONFLICT(user,character,day) DO UPDATE SET last_at=MAX(visits.last_at,excluded.last_at)', (key,user,legacy))
            db.execute('DELETE FROM visits WHERE user=? AND character=?', (user,legacy))

    def anniversary(self, user, name=None, day=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            if name is not None:
                if not name.strip() or len(name) > 40:
                    raise ValueError('名称需要1至40个字')
                date.fromisoformat('2000-' + str(day))
                if len(str(day)) != 5:
                    raise ValueError('日期格式应为 MM-DD')
                db.execute('INSERT OR REPLACE INTO anniversaries VALUES (?,?,?)', (str(user), name.strip(), day))
            return db.execute('SELECT name,day FROM anniversaries WHERE user=? ORDER BY day,name', (str(user),)).fetchall()

    def forget(self, user, name):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute('DELETE FROM anniversaries WHERE user=? AND name=?', (str(user), name)).rowcount
