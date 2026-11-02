"""Deterministic clock facts and timestamped conversation serialization."""
from datetime import datetime
import re

WEEKDAYS = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')


def period(hour):
    if hour < 5:
        return '凌晨'
    if hour < 8:
        return '清晨'
    if hour < 11:
        return '上午'
    if hour < 13:
        return '中午'
    if hour < 18:
        return '下午'
    return '晚上'


def clock_context(current, history):
    facts = [f'当前现实时间：{current:%Y-%m-%d %H}时（{current.tzinfo}，{WEEKDAYS[current.weekday()]}，{period(current.hour)}）。',
             '时间以本轮时钟为准，历史早晚不可覆盖；只精确到小时。当前时间/日期用{当前时间}/{当前日期}，不无故催睡。']
    last = next((m.get('at') for m in reversed(history) if m.get('role') == 'user' and m.get('at')), None)
    if last:
        try:
            previous = datetime.fromisoformat(last)
            if previous.tzinfo:
                delta = max(0, int((current-previous).total_seconds()//3600))
                facts.append(f'上次用户发言：{previous:%m-%d %H}时，约{delta}小时前。')
        except ValueError:
            pass
    return '\n'.join(facts)


def api_history(history, assistant_json=False):
    import json
    result = []
    for message in history[-12:]:
        if message.get('role') not in ('user', 'assistant'):
            continue
        stamp = '历史'
        if message.get('at'):
            try:
                stamp = datetime.fromisoformat(message['at']).strftime('%m-%d %H时')
            except ValueError:
                pass
        content=f'[{stamp}] {message["content"]}'
        if assistant_json and message['role']=='assistant':
            # Match the required output envelope. A timestamp-prefixed assistant
            # example otherwise invites the model to continue that plain-text form.
            content=json.dumps({'reply':message['content']},ensure_ascii=False)
        result.append({'role': message['role'], 'content':content})
    return result


def time_question(text):
    clean = re.sub(r'[\s，。！？?！～~]+', '', text)
    return clean in ('/时间', '现在几点', '现在几点了', '几点了', '几点', '现在是什么时间',
                     '现在的时间', '你那里几点', '你那边几点', '你那边现在几点',
                     '今天几号', '今天是几号', '今天星期几', '现在是几月几号', '今天什么日期')


def time_answer(current):
    zone = '北京时间' if str(current.tzinfo) == 'Asia/Shanghai' else str(current.tzinfo)
    return f'现在是{zone} {current:%H}时，{current:%Y年%m月%d日}，{WEEKDAYS[current.weekday()]}。'


def render_time(text, current):
    text = text.replace('{当前时间}', current.strftime('%H时')).replace('{当前日期}', current.strftime('%Y年%m月%d日'))
    # Enforce explicit present-clock claims even if the model ignores placeholders.
    pattern = r'((?:现在|此刻|当前|这会儿)(?:已经|才|是|都)?(?:北京时间)?\s*)(?:\d{1,2}[：:]\d{2}(?:[：:]\d{2})?|[零一二两三四五六七八九十\d]{1,3}点(?:[零一二两三四五六七八九十\d]{1,3}分|半|整)?)'
    text = re.sub(pattern, lambda m: m.group(1) + current.strftime('%H时'), text)
    text = re.sub(r'(现在(?:已经|是)?)(?:凌晨|清晨|上午|中午|下午|晚上)', lambda m: m.group(1)+period(current.hour), text)
    return text


def interaction(message, history):
    recent = [m.get('content','') for m in history[-8:]]
    feedback = bool(re.search(r'模仿|学你|你(?:的)?.{0,6}(?:口气|语气|说话)|话里有话|反话|讽刺|阴阳',message))
    joke_end = bool(re.search(r'不好笑|没意思|别解释|算了|笑话.{0,4}(?:冷|尬)',message))
    recursive = bool(re.search(r'自己|自画像|头像',message) and re.search(r'画|头像',message) and
                     (re.search(r'又|还|怎么|怪|套娃',message) or any('画' in t for t in recent[-3:])))
    return {'intent':'closing' if joke_end else 'teasing' if feedback or recursive else None,
            'meta':feedback,'recursive':recursive,'joke_end':joke_end,
            'recent':recent[-4:]}


def interaction_context(message, history):
    value=interaction(message,history)
    if not value['intent']:
        return ''
    return ('本轮互动线索：'+str({k:v for k,v in value.items() if k!='recent'})+
            '。结合最近几轮先判断话语指向谁、是否引用你先前的口吻。调侃可接梗或轻微回击，'
            '不当成新的事实，不盘问感受。自我指涉的画面允许觉得滑稽，不必辩护合理性。'
            '笑话冷场就自然收住。若被追问理解，给出有上下文依据的具体理解；不确定就承认，'
            '不要只说看出来了。冷淡不等于每轮反驳，情绪变化要有本轮原因。')
