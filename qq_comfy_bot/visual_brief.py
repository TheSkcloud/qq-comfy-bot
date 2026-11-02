"""Persistent outfit/setting vocabulary; action and framing remain free to change."""
import re
from companion import character_key

FIELDS = ('identity', 'outfit', 'setting', 'action', 'composition')


def usable_tags(value):
    return isinstance(value,str) and 0<len(value.strip())<=1200 and not re.search(r'[\u4e00-\u9fff]',value)


def complete_from_cache(session, data):
    """Reuse only vocabulary whose saved source still matches this exact scene."""
    brief = validate_brief(data.get('visual_scene'))
    cache = session.get('visual_cache', {})
    state = data['state']
    if cache.get('identity') == normalize(state['character']):
        if usable_tags(cache.get('identity_tags')):
            brief['identity'] = cache['identity_tags']
        for field, source in (('outfit', 'clothing'), ('setting', 'location')):
            item = cache.get(field, {})
            if item.get('source') == normalize(state[source]) and usable_tags(item.get('tags')):
                brief[field] = item['tags']
    plan = data.get('image_plan') or {}
    if isinstance(plan, dict):
        for field, source in (('action', 'action'), ('composition', 'camera')):
            if not brief.get(field) and isinstance(plan.get(source), str):
                brief[field] = plan[source]
    data['visual_scene'] = brief
    return [field for field in FIELDS if not usable_tags(brief.get(field))]


def normalize(value):
    return re.sub(r'[\s，,。；;、]+', '', value).casefold()


def validate_brief(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('visual_scene must be an object')
    result = {}
    for field in FIELDS:
        tags = value.get(field, '')
        if not isinstance(tags, str) or len(tags) > 1200:
            raise ValueError('Invalid visual_scene.' + field)
        result[field] = tags.strip()
    return result


def cache_context(session):
    cache = session.get('visual_cache', {})
    state = session['state']
    if cache.get('identity') != normalize(state['character']):
        return ''
    parts = []
    if usable_tags(cache.get('identity_tags')):
        parts.append('当前人设的固定外貌绘图标签：'+cache['identity_tags'])
    for field, label, source in [('outfit','同一套服装的固定绘图标签','clothing'), ('setting','同一地点的固定绘图标签','location')]:
        item = cache.get(field, {})
        if item.get('source') == normalize(state[source]) and usable_tags(item.get('tags')):
            parts.append(label+'：'+item['tags'])
    return '\n'.join(parts)


def assemble(session, data):
    state = data['state']
    brief = validate_brief(data.get('visual_scene'))
    if not all(usable_tags(brief.get(field)) for field in FIELDS):
        # Older model outputs remain usable; never cache a whole legacy prompt as clothing.
        return data['visual_tags']
    cache = session.get('visual_cache', {})
    identity = normalize(state['character'])
    if cache.get('identity') != identity:
        cache = {'identity':identity, 'character_id':character_key(state['character'])}
    if not usable_tags(cache.get('identity_tags')):cache['identity_tags']=brief['identity']
    tags = [cache['identity_tags']]
    for field, source in [('outfit','clothing'), ('setting','location')]:
        item = cache.get(field, {})
        source_key = normalize(state[source])
        if item.get('source') != source_key or not usable_tags(item.get('tags')):
            item = {'source':source_key, 'tags':brief[field]}
            cache[field] = item
        tags.append(item['tags'])
    tags.extend([brief['action'], brief['composition']])
    session['visual_cache'] = cache
    return ', '.join(tags)
