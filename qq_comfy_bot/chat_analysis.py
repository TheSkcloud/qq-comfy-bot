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
memory_updates 最多四项 {kind:location/activity/preference/plan/basic_fact,value,evidence:本轮用户连续原话}，无事实填 []。排除猜测、玩笑、角色活动、第三人活动、引用中他人的自称和私密凭据。识别引语说话人、反问、夸张与不同评价维度；它们不必然是用户在向角色提问或自相矛盾。
dialogue_update:{relation:continue/switch/side/resume,topic_summary,evidence:本轮用户连续原话,important_context:[{speaker:user,text,evidence}],open_questions:[{question,evidence}]}。只整理有证据的上下文，当前问题还没答就不记为已解决。可提供 emotion_state:{category,intensity,evidence}、user_intent、trigger_actor、trigger_evidence、interaction_event、affect。用户明确要图时可给 image_request:{action:request,evidence:本轮用户连续原话}，只识别请求意图。"""


def route(text, discussion=None, history=()):
    text = text.strip()
    try:
        minimum = int(os.getenv('CHAT_SPLIT_MIN_CHARS', '240'))
    except ValueError:
        minimum = 240
    if minimum <= 0:
        return False, 'disabled'
    if not text or text.startswith('/') or re.fullmatch(r'[嗯哦噢好哈呵嘿啊呀!！。～~\s]+', text):
        return False, 'local_or_ack'
    if len(text) > 30 and len(set(text)) < 8:
        return False, 'repetition'
    if re.search(r'你是不是.*(?:讨厌|生气|不喜欢)|我觉得你变了|误解|误会|我们的关系|你的边界', text):
        return True, 'relationship_reasoning'
    if re.search(r'你还记得|我们上次|刚才.*(?:为什么|意思|说)|那天.*(?:说|事)|之前.*(?:矛盾|不是)', text):
        return True, 'historical_reference'
    if discussion and discussion.get('open_questions') and re.search(r'为什么|所以|但是|接着|继续', text):
        return True, 'unresolved_discussion'
    if history and re.search(r'^(?:所以|但是|那么|这|那|他|她|它).*(?:为什么|怎么|吗|呢|意思|矛盾)|不尽人意|和解|反过来',text):
        return True, 'contextual_reasoning'
    if len(re.findall(r'[“「]',text))>=1 and re.search(r'他|她|老师|同学|朋友',text):
        return True, 'quoted_speaker'
    return (True, 'length_fallback') if len(text) >= minimum else (False, 'simple')


def should_split(text):
    return route(text)[0]


async def analyze(http, user, history, local_context, discussion):
    recent = [{'role': item['role'], 'content': item.get('content', '')[:480]}
              for item in history[-4:] if item.get('role') in ('user', 'assistant')]
    local_context = local_context[:2400]
    topic = {'summary': discussion.get('topic_summary', '')[:300],
             'open_questions': discussion.get('open_questions', [])[-3:]}
    system = SYSTEM
    import capability_registry,preferences
    if capability_registry.resolve(preferences.load()).permits('character_auto'):
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
