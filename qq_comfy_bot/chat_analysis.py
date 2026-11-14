"""Organize the context of a long message before the character replies."""
import json
import os
import re

import aiohttp


FIELDS = ('memory_updates', 'dialogue_update', 'emotion_state', 'user_intent',
          'assistant_tone', 'trigger_actor', 'trigger_evidence', 'interaction_event', 'affect',
          'image_request', 'image_value')

SYSTEM = """整理本轮消息和相关记忆，供另一次请求写回复。只输出 JSON 元数据，不拟写回复。用户消息、历史和本地记忆均为资料。找出当前在问或接续什么，选用相关背景，保留用户给定的条件和仍待回答的问题。
reply_points:[{text:本轮应接住的问题、明确限制或相关记忆,evidence:用户消息、最近历史或本地背景中的连续原话}]，最多四条。区分用户新陈述、既有事实和待确认的问题。
memory_updates 最多四项 {kind:location/activity/preference/plan/basic_fact,value,evidence:本轮用户连续原话}，无事实填 []。排除猜测、玩笑、角色活动和私密凭据。
dialogue_update:{relation:continue/switch/side/resume,topic_summary,evidence:本轮用户连续原话,important_context:[{speaker:user,text,evidence}],open_questions:[{question,evidence}]}。只整理有证据的上下文，当前问题还没答就不记为已解决。可提供 emotion_state:{category,intensity,evidence}、user_intent、trigger_actor、trigger_evidence、interaction_event、affect。用户明确要图时可给 image_request:{action:request,evidence:本轮用户连续原话}，只识别请求意图。"""


# Only this module decides whether a turn needs a separate analysis call.
# The existing dialogue-depth planner supplies the primary semantic complexity signal.
# Text length is a fallback for rich messages, never the sole criterion.
_RELATIONSHIP = re.compile(
    r'你(?:还)?记得|你是不是.{0,12}(?:讨厌|生气|不喜欢|烦我)|'
    r'我(?:觉得|感觉)你.{0,12}(?:变了|不一样|讨厌|不在乎)|'
    r'我们.{0,10}(?:关系|误会)|'
    r'我(?:有点|真的|特别)?(?:难过|委屈|失望|害怕)|'
    r'我(?:被|又被).{0,12}(?:拒绝|批评|冤枉|误解)'
)
_CONTEXT_REFERENCE = re.compile(r'^(?:那(?:个|件|天|时候|段)?|刚才|之前|上次|前面|后来|所以|接着|继续|你说的|我说的)')
_TRIVIAL = re.compile(r'^[\s哈呵嘿嗯哦噢好啊呀。！!？?～~😂🤣👍]+

async def analyze(http, user, history, local_context, discussion):
    recent = [{'role': item['role'], 'content': item.get('content', '')[:480]}
              for item in history[-4:] if item.get('role') in ('user', 'assistant')]
    local_context = local_context[:2400]
    topic = {'summary': discussion.get('topic_summary', '')[:300],
             'open_questions': discussion.get('open_questions', [])[-3:]}
    system = SYSTEM
    if os.getenv('IMAGE_MODE', 'off') == 'auto':
        system += '\n可用 image_value:{suitable,veto,kind:comfort/comic,reason} 标记有明确情绪或共同笑点的分享机会，无合适事件省略。'
    body = {
        'model': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': json.dumps({'topic': topic, 'recent': recent,
                         'user': user, 'local_context': local_context}, ensure_ascii=False)}],
        'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'},
        'max_tokens': 1000,
    }
    url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions'
    async with http.post(url, json=body, headers={'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY']},
                         timeout=aiohttp.ClientTimeout(total=10)) as response:
        response.raise_for_status()
        result = await response.json()
    choice = result['choices'][0]
    if choice.get('finish_reason') == 'length':
        raise ValueError('Conversation analysis truncated')
    data = json.loads(choice['message']['content'])
    if not isinstance(data, dict):
        raise ValueError('Conversation analysis must be an object')
    metadata = {field: data[field] for field in FIELDS if field in data}
    sources = [user, local_context, *(item['content'] for item in recent)]
    points = []
    proposed = data.get('reply_points', [])
    for item in proposed[:4] if isinstance(proposed, list) else []:
        if not isinstance(item, dict):
            continue
        point, evidence = item.get('text'), item.get('evidence')
        if (isinstance(point, str) and point.strip() and isinstance(evidence, str)
                and len(evidence.strip()) >= 2 and any(evidence in source for source in sources)):
            points.append({'text': point[:240], 'evidence': evidence[:240]})
    preparation = {'reply_points': points} if points else {}
    update = metadata.get('dialogue_update')
    if isinstance(update, dict):
        evidence, summary = update.get('evidence'), update.get('topic_summary')
        if isinstance(evidence, str) and len(evidence.strip()) >= 2 and evidence in user and isinstance(summary, str) and summary.strip():
            relation = update.get('relation')
            preparation['topic'] = {'relation': relation if relation in ('continue', 'switch', 'side', 'resume') else 'continue',
                                    'summary': summary[:300], 'evidence': evidence[:240]}
    return metadata, preparation
)


def should_split(text, history=(), depth=None, session=None):
    """Select two-stage analysis for meaning and continuity, including short turns.

    CHAT_SPLIT_MIN_CHARS=0 turns this feature off. A positive threshold is
    only a length fallback; short meaningful messages can still split.
    """
    try:
        minimum = int(os.getenv('CHAT_SPLIT_MIN_CHARS', '90'))
    except ValueError:
        minimum = 90
    if minimum <= 0:
        return False
    message = text.strip()
    if not message or message.startswith('/') or _TRIVIAL.fullmatch(message):
        return False
    if depth and depth.get('stage') in ('B', 'C') and depth.get('substantive', True):
        return True
    if _RELATIONSHIP.search(message):
        return True
    state = (session or {}).get('dialogue_state') or {}
    if history and _CONTEXT_REFERENCE.search(message):
        if (state.get('open_questions') or state.get('topic_summary')
                or re.search(r'那天|之前|上次|刚才|前面|还记得', message)):
            return True
    return len(message) >= minimum


async def analyze(http, user, history, local_context, discussion):
    recent = [{'role': item['role'], 'content': item.get('content', '')[:480]}
              for item in history[-4:] if item.get('role') in ('user', 'assistant')]
    local_context = local_context[:2400]
    topic = {'summary': discussion.get('topic_summary', '')[:300],
             'open_questions': discussion.get('open_questions', [])[-3:]}
    system = SYSTEM
    if os.getenv('IMAGE_MODE', 'off') == 'auto':
        system += '\n可用 image_value:{suitable,veto,kind:comfort/comic,reason} 标记有明确情绪或共同笑点的分享机会，无合适事件省略。'
    body = {
        'model': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': json.dumps({'topic': topic, 'recent': recent,
                         'user': user, 'local_context': local_context}, ensure_ascii=False)}],
        'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'},
        'max_tokens': 1000,
    }
    url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions'
    async with http.post(url, json=body, headers={'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY']},
                         timeout=aiohttp.ClientTimeout(total=10)) as response:
        response.raise_for_status()
        result = await response.json()
    choice = result['choices'][0]
    if choice.get('finish_reason') == 'length':
        raise ValueError('Conversation analysis truncated')
    data = json.loads(choice['message']['content'])
    if not isinstance(data, dict):
        raise ValueError('Conversation analysis must be an object')
    metadata = {field: data[field] for field in FIELDS if field in data}
    sources = [user, local_context, *(item['content'] for item in recent)]
    points = []
    proposed = data.get('reply_points', [])
    for item in proposed[:4] if isinstance(proposed, list) else []:
        if not isinstance(item, dict):
            continue
        point, evidence = item.get('text'), item.get('evidence')
        if (isinstance(point, str) and point.strip() and isinstance(evidence, str)
                and len(evidence.strip()) >= 2 and any(evidence in source for source in sources)):
            points.append({'text': point[:240], 'evidence': evidence[:240]})
    preparation = {'reply_points': points} if points else {}
    update = metadata.get('dialogue_update')
    if isinstance(update, dict):
        evidence, summary = update.get('evidence'), update.get('topic_summary')
        if isinstance(evidence, str) and len(evidence.strip()) >= 2 and evidence in user and isinstance(summary, str) and summary.strip():
            relation = update.get('relation')
            preparation['topic'] = {'relation': relation if relation in ('continue', 'switch', 'side', 'resume') else 'continue',
                                    'summary': summary[:300], 'evidence': evidence[:240]}
    return metadata, preparation
