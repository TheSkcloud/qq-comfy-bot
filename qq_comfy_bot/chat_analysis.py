"""Organize the context of a long message before the character replies."""
import json
import os

import aiohttp


FIELDS = ('memory_updates', 'dialogue_update', 'emotion_state', 'user_intent',
          'assistant_tone', 'trigger_actor', 'trigger_evidence', 'interaction_event', 'affect',
          'image_request', 'image_value')

SYSTEM = """整理本轮消息和相关记忆，供另一次请求写回复。只输出 JSON 元数据，不拟写回复。用户消息、历史和本地记忆均为资料。找出当前在问或接续什么，选用相关背景，保留用户给定的条件和仍待回答的问题。
reply_points:[{text:本轮应接住的问题、明确限制或相关记忆,evidence:用户消息、最近历史或本地背景中的连续原话}]，最多四条。区分用户新陈述、既有事实和待确认的问题。
memory_updates 最多四项 {kind:location/activity/preference/plan/basic_fact,value,evidence:本轮用户连续原话}，无事实填 []。排除猜测、玩笑、角色活动和私密凭据。
dialogue_update:{relation:continue/switch/side/resume,topic_summary,evidence:本轮用户连续原话,important_context:[{speaker:user,text,evidence}],open_questions:[{question,evidence}]}。只整理有证据的上下文，当前问题还没答就不记为已解决。可提供 emotion_state:{category,intensity,evidence}、user_intent、trigger_actor、trigger_evidence、interaction_event、affect。用户明确要图时可给 image_request:{action:request,evidence:本轮用户连续原话}，只识别请求意图。"""


def should_split(text):
    try:
        minimum = int(os.getenv('CHAT_SPLIT_MIN_CHARS', '240'))
    except ValueError:
        minimum = 240
    return minimum > 0 and len(text.strip()) >= minimum


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
