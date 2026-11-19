"""Turn timing and durable segment plans. Transport stays in Bot.send."""
from contextvars import ContextVar
from datetime import timedelta
import random
import re
import uuid


SEND_ECHO = ContextVar('turn_send_echo', default=None)

DEFAULTS = {
    'enabled': True,
    'input_collector': {'enabled': True, 'quiet_window_ms': 1200, 'max_wait_ms': 3000},
    'staged_reply': {'enabled': True, 'max_segments': 3},
    'natural_pause': {'enabled': True, 'min_ms': 350, 'max_ms': 1000},
    'interruption': {'enabled': True},
    'recovery': {'enabled': True},
    'episode_ttl_seconds': 150,
}
BOUNDS = {'quiet_window_ms': (0, 5000), 'max_wait_ms': (0, 10000),
          'max_segments': (1, 4), 'min_ms': (0, 5000), 'max_ms': (0, 8000),
          'episode_ttl_seconds': (5, 600)}
BEHAVIORS = {'reaction', 'acknowledgement', 'answer', 'followup', 'question', 'advice', 'closing'}


def config(values=None, defaults=None):
    defaults = DEFAULTS if defaults is None else defaults
    values = values if isinstance(values, dict) else {}
    result = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        if isinstance(default, dict):
            result[key] = config(value, default)
        elif type(default) is bool:
            result[key] = value if type(value) is bool else default
        else:
            lower, upper = BOUNDS[key]
            result[key] = max(lower, min(upper, value)) if type(value) is int else default
    return result


def remaining_window(queue, current, cfg, previous_gap=None):
    if not cfg['enabled'] or not queue:
        return 0
    if any(text.startswith('/') or event.get('_explicit_image_request') or
           text in ('取消', '停止回复', '别回复了') for event, text in queue):
        return 0
    first = queue[0][0]['_queued_at']
    event, text = queue[-1]
    last = event['_queued_at']
    quiet = cfg['quiet_window_ms'] / 1000
    if previous_gap is not None and len(queue) == 1:
        quiet = max(quiet, min(quiet * 1.3, previous_gap * 1.4))
    if len(queue) > 1:
        gap = last - queue[-2][0]['_queued_at']
        quiet = max(quiet * .6, min(quiet * 1.5, gap * 1.4))
    if re.search(r'(等等|还有|再补充一下|我跟你说|[，、…])$', text.strip()):
        quiet *= 1.4
    return max(0, min(first + cfg['max_wait_ms'] / 1000, last + quiet) - current)


def pause_seconds(cfg, behavior, pace):
    low = cfg['min_ms']
    high = max(low, cfg['max_ms'])
    weight = .3 if behavior in ('answer', 'closing') or pace == 'fast' else .8 if pace == 'emotion' else .55
    return random.triangular(low, high, low + (high - low) * weight) / 1000


def episode(plan, source, ticket, cfg, current):
    texts = list(plan['segments'])
    priorities = list(plan.get('segment_priority', []))
    priorities += ['optional'] * (len(texts) - len(priorities))
    types = plan.get('segment_types') or []
    limit = cfg['staged_reply']['max_segments'] if cfg['enabled'] and cfg['staged_reply']['enabled'] else 1
    if len(texts) > limit:
        texts[limit-1:] = ['\n'.join(texts[limit-1:])]
        priorities[limit-1:] = ['essential' if 'essential' in priorities[limit-1:] else 'optional']
    identifier = uuid.uuid4().hex
    fallback = {'acknowledge': 'acknowledgement', 'teasing': 'reaction', 'emotion': 'reaction',
                'advice': 'advice', 'closing': 'closing', 'question': 'answer'}.get(plan['intent'], 'answer')
    segments = []
    for index, text in enumerate(texts):
        behavior = types[index] if index < len(types) else fallback if index == 0 else 'followup'
        segments.append({'index': index, 'id': f'{identifier}:{index}', 'text': text,
                         'type': behavior, 'priority': priorities[index], 'status': 'queued',
                         'continue_policy': 'reevaluate' if index and priorities[index]=='optional' else 'send'})
    return {'id': identifier, 'version': ticket.version if ticket else 0, 'source': source,
            'mode': 'STAGED' if len(segments) > 1 else 'SINGLE', 'segments': segments,
            'expires_at': (current + timedelta(seconds=cfg['episode_ttl_seconds'])).isoformat()}


def omit_reason(item, source, acknowledged, history):
    if item.get('continue_policy') != 'reevaluate' or not acknowledged:
        return ''
    import speech
    normalize = lambda text: re.sub(r'[\s。！？!?，,]+', '', text)
    if normalize(item['text']) in {normalize(text) for text in acknowledged}:
        return 'already_said'
    if speech.EXIT.search(source) or re.search(r'不说了|到此为止',source):
        return 'topic_closed'
    mode = speech.plan(source, history)['mode']
    advice = item['type']=='advice' or re.match(r'(?:建议你|你最好|下次你可以)',item['text'])
    if advice and mode!='help' and (mode=='vent' or re.search(r'只想.{0,4}(?:说|吐槽)|不要建议|别教我',source)):
        return 'listening'
    if speech.question_count(item['text']) and (speech.NO_QUESTIONS.search(source) or
                                                any(speech.question_count(text) for text in acknowledged)):
        return 'unnecessary_question'
    return ''


def settle(plan, reason, receipts=()):
    """Only an ACK matching the episode and segment can resolve an in-flight send."""
    changed = False
    for segment in plan.get('segments', []):
        if segment.get('status') == 'queued':
            segment.update(status='cancelled' if plan.get('id') else 'unknown', reason=reason)
        elif segment.get('status') in ('sending', 'unknown'):
            ack = next((r.get('qq_result') for r in receipts if plan.get('id') and
                        r.get('episode_id') == plan['id'] and r.get('segment') == segment['index']
                        and (r.get('qq_result') or {}).get('message_id')), None)
            if ack:
                segment.update(status='sent', qq_result=ack)
            elif segment['status'] == 'sending':
                segment.update(status='unknown', reason=reason)
            else:
                continue
        else:
            continue
        changed = True
    return changed
