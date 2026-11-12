import json
from pathlib import Path

PATH = Path(__file__).with_name('preferences.json')
DEFAULT = dict(proactive_enabled=False, reactions=True, image_chat=True, image_chat_interval=10, image_chat_max=4, notes_auto=True, romance_mode='slow', spontaneous_images=True,thinking_after_substantive_turns=4)


def load():
    try:
        from image_director import config
        result = {**DEFAULT, **json.loads(PATH.read_text(encoding='utf-8'))}
        result['thinking_after_substantive_turns']=max(1,min(20,int(result['thinking_after_substantive_turns'])))
        result['drawing'] = config(result.get('drawing'))
        from reply_scheduler import timing_config
        result['reply_timing']=timing_config(result.get('reply_timing'))
        return result
    except (OSError, ValueError):
        from image_director import config
        return {**DEFAULT, 'drawing':config()}


def save(values):
    previous=load()
    drawing={**previous.get('drawing',{}),**values.get('drawing',{})} if isinstance(values.get('drawing',{}),dict) else values.get('drawing')
    values = {**previous, **values, 'drawing':drawing}
    mode = values.get('romance_mode', 'slow')
    if mode not in ('slow', 'friends', 'partner'):
        raise ValueError('Unknown relationship mode')
    clean = dict(proactive_enabled=bool(values.get('proactive_enabled',False)), romance_mode=mode, spontaneous_images=bool(values.get('spontaneous_images', True)), reactions=bool(values.get('reactions', True)), image_chat=bool(values.get('image_chat', True)), notes_auto=bool(values.get('notes_auto',True)),
                 image_chat_interval=max(5, min(30, int(values.get('image_chat_interval', 10)))),
                 image_chat_max=max(0, min(4, int(values.get('image_chat_max', 4)))))
    from image_director import config
    clean['drawing'] = config(values.get('drawing'))
    from reply_scheduler import timing_config
    clean['reply_timing']=timing_config(values.get('reply_timing'))
    clean['thinking_after_substantive_turns']=max(1,min(20,int(values.get('thinking_after_substantive_turns',4))))
    temp = PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(PATH)
