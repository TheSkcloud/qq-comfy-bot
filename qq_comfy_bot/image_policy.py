"""Image intent and bounded in-chat sharing; no unsolicited background sending."""
from datetime import datetime
import re


VISUAL_TOPIC = r'跳舞|舞步|舞蹈|练功服|海报|照片|图片|插画|自拍|头像|穿搭|衣裙|服装|发型|造型|样貌|模样|弹琴|做饭|散步|读书|看书|画画|(?:学习|写字|做笔记)(?:的时候|时候|时)?的?(?:样子|模样)'
NON_VISUAL_TARGET = r'怎么看|看法|观点|想法|思路|原理|算法|源码|报错|日志|函数|公式|解题|代码|实现|文档|后续发展|接下来会|结果如何'


def visual_reference(history):
    """Resolve a short 'may I see?' only within the recent, still relevant visual topic."""
    recent=[m for m in (history or [])[-6:] if isinstance(m,dict)]
    if recent and recent[-1].get('at'):
        try:
            stamp=datetime.fromisoformat(recent[-1]['at'])
            if stamp.tzinfo and (datetime.now(stamp.tzinfo)-stamp).total_seconds()>900:return False
        except (ValueError,TypeError):pass
    for message in reversed(recent):
        content=message.get('content','')
        if not isinstance(content,str):continue
        if re.search(NON_VISUAL_TARGET,content):return False
        if re.search(VISUAL_TOPIC,content):return True
        if message.get('role')=='user' and len(content)>5 and not re.search(r'看看|想看|什么样|啥样|就那样',content):
            return False  # A newer substantive topic ends the older visual reference.
    return False


def visual_request_context(text,history=None):
    """Keep the referenced artifact/subject with a short visual follow-up."""
    reference=text if re.search(VISUAL_TOPIC,text) else ''
    if not reference and visual_reference(history):
        reference=next((m.get('content','') for m in reversed((history or [])[-6:])
                        if re.search(VISUAL_TOPIC,str(m.get('content','')))), '')
    purpose='poster' if '海报' in reference else 'avatar' if '头像' in reference else 'illustration'
    instruction='请绘制这个视觉主体，保留当前角色、服装和地点；不要只生成无关的普通肖像。'
    if purpose=='poster':instruction+='本轮要看角色海报：绘制角色海报式插画与海报构图，不必让角色手持纸张，不编造已经持有的真实官方海报。'
    return {'reference':reference[:600],'purpose':purpose,'instruction':instruction}


def image_requested(text, history=None):
    if text.startswith('/拍照'):
        return True
    if re.search(r'(?:不要|别|不用|不想|不需要|先不|别再|不聊|不说|不看).{0,8}(?:图|照片|拍照|拍张照|画|给我看|看你|看|海报)', text):
        return False
    strong=bool(re.search(r'画(?:一|两|二|三|个|张|幅|点)|发.{0,3}(?:图|照片)|拍.{0,3}(?:照|相)',text))
    # A short imagined view can omit both "show me" and a question mark.
    # Keep this narrow: named role + visible activity + appearance, without an
    # evaluative continuation ("很认真") or technical/psychological question.
    subject=r'(?:你|铃木(?:羽那)?|鈴木(?:羽那)?|羽那|Suzuki\s*Hana|Hana)'
    activity=r'(?:学习|读书|看书|写字|做笔记|上课|练习|练舞|跳舞|弹琴|画画|做饭|散步|跑步|穿衣|换装)'
    if not re.search(NON_VISUAL_TARGET,text) and re.fullmatch(
            subject+r'(?:在|正在)?'+activity+r'(?:的时候|时候|时|起来)?的?(?:样子|模样)[\s。.…！？?!～~👀🤔]*',text,re.I):return True
    # Asking what the character physically looks like is a visual request, not a
    # demand that the user first say the words "draw" or "photo".
    appearance=bool(re.search(r'你[^，。！？?!\n]{0,22}(?:的时候|时候|起来|时)(?:会|是|会是|大概是)?(?:什么|啥)(?:样子|模样|样)',text))
    if appearance and not re.search(r'感觉|心情|体验|想法|思路|算法|程序运行|解决方案',text):return True
    if not strong and re.search(NON_VISUAL_TARGET,text):return False
    clean = re.sub(r'[^\w\u4e00-\u9fff/]+', '', text)
    looking=bool(re.search(r'(?:想|想要|让我|给我|我可以|我能|能不能|可不可以|可以).{0,3}看(?:看|一眼|一下)?|看看(?:吗|呗|嘛)',text))
    if looking:
        if re.search(r'(?:想|想要|让我|给我|我可以|我能|能不能|可不可以|可以).{0,3}看(?:看|一眼|一下)?你',text):return True
        if re.search(VISUAL_TOPIC,text) or visual_reference(history):return True
    if clean in ('我看看', '让我看看', '给我看看', '看看', '我想看看', '照片', '发图'):
        if history and not visual_reference(history) and any(re.search(NON_VISUAL_TARGET,str(m.get('content',''))) for m in history[-3:]):return False
        return True
    if re.search(r'拍(?:[一二两三123])?张.{0,6}(?:照片|照|相)|照张相|(?:想看|看看|给我|发|来).{0,8}照片',text):
        return True
    return any(word in text for word in ('发图', '发张图', '发一张图', '拍照', '看看你', '给我看', '让我看', '画一张', '画张', '画个', '画一个', '画幅', '画一幅', '画点', '再来一张', '再拍一张'))


def picture_count(text):
    match = re.search(r'([一二两三123])\s*张', text)
    return {'一': 1, '二': 2, '两': 2, '三': 3, '1': 1, '2': 2, '3': 3}.get(match.group(1), 1) if match else 1


def allow_spontaneous(session, text, current, enabled=True):
    if not enabled or not 8 <= current.hour < 23:
        return False
    if re.search(r'不要.*(?:图|照片)|别.*(?:图|照片)|不想|难过|烦死|累死|报错|不开心|忙|上课|学习|作业|晚点聊|先走|睡了', text):
        return False
    recent = []
    for stamp in session.get('image_events', []):
        try:
            value = datetime.fromisoformat(stamp)
            if value.tzinfo is not None and value.date() == current.date():
                recent.append(value)
        except (ValueError, TypeError):
            continue
    if len(recent) >= 3:
        return False
    last = session.get('last_image_sent_at')
    if last:
        try:
            if (current - datetime.fromisoformat(last)).total_seconds() < 45 * 60:
                return False
        except (ValueError, TypeError):
            pass
    return True


def scene_tags(tags, hour, character=''):
    """Keep style in the workflow, identity in the card, and lighting in the clock."""
    from companion import load_card
    identity = load_card(character).get('visual_identity', '')
    # Explicit alternate hair/eye descriptions in the identity override the local card.
    if identity and any(word in character for word in ('黑发', '金发', '短发', '红发', '绿眼', '蓝眼', '黑眼')):
        identity = ''
    temporal = ('daytime', 'night', 'midnight', 'morning', 'afternoon', 'evening', 'sunset', 'sunrise', 'dawn', 'dusk', 'sunlight', 'daylight', 'moonlight', 'dark sky')
    clean = []
    for tag in tags.split(','):
        tag = tag.strip()
        low = tag.lower()
        if not tag or any(word in low for word in temporal):
            continue
        if identity and re.search(r'\b(?:hair|eyes)\b', low):
            continue
        if low not in {value.lower() for value in clean}:
            clean.append(tag)
    light = ('early morning, soft morning light' if 5 <= hour < 8 else
             'daytime, natural daylight' if 8 <= hour < 17 else
             'evening, warm dusk light' if 17 <= hour < 19 else
             'night, ambient artificial lighting')
    # Clock lighting is compatible with indoors and does not force a visible sky.
    return ', '.join(([identity] if identity else []) + clean + [light])
