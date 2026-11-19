"""Persistent outfit/setting vocabulary; action and framing remain free to change."""
import re
from companion import character_key, load_card
from image_policy import excluded_objects

FIELDS = ('identity', 'outfit', 'setting', 'action', 'composition')

# These are reusable local poses and outfits for the persisted fictional routine.
# The saved character identity and any manually selected clothing/location take priority.
ROUTINE_LOOKS = (
    (('散步','逛街'), '附近步道', 'park path, leafy trees', 'light casual jacket, simple trousers', 'walking along the path', ''),
    (('咖啡馆',), '咖啡馆', 'quiet cafe, window seat', 'casual cardigan, plain shirt', 'holding a cup near the window', 'cup'),
    (('做饭','早餐','午饭','晚饭','吃饭'), '家里的餐桌', 'home dining table, chairs', 'casual home shirt', 'sitting at the table', ''),
    (('学习','读书','看书','工作','笔记'), '家里的书桌旁', 'home room, desk by window', 'casual cardigan, plain shirt', 'reading an open book', 'open book'),
    (('跑步','慢跑'), '附近步道', 'outdoor path', 'simple athletic top, running shorts', 'mid-stride with her lead foot just touching down as her arms swing', ''),
    (('拉伸','伸展'), '家里的空地', 'home room, open floor', 'simple athletic top, loose trousers', 'shifting her weight into a gentle side stretch, one elbow arcing above her shoulder', ''),
    (('运动','练习'), '家里的空地', 'home room, open floor', 'simple athletic top, loose trousers', 'steadying her balance after a movement, shoulders relaxing as she exhales', ''),
    (('睡觉','睡前','休息','洗漱'), '家里的房间', 'quiet home room, armchair', 'simple lounge clothes', 'sitting quietly in a chair', ''),
)
DEFAULT_LOOK = ('家里的房间', 'quiet home room, window', 'comfortable home clothes', 'pausing by the window', '')


def simple_photo(text):
    return bool(re.fullmatch(r'(?:/拍照|拍(?:一)?张(?:照|照片)(?:给我)?|(?:发|来)(?:一)?张(?:你的)?(?:照片|自拍)|给我拍张照)[。！!～~\s]*',text.strip()))


def local_plan(session, activity, defaults):
    state=session['state']
    cache=session.get('visual_cache',{})
    if cache.get('identity')!=normalize(state['character']):cache={}
    card=load_card(state['character'])
    identity=(card.get('visual_identity') if state['character']==defaults['character'] else None) or cache.get('identity_tags')
    if not usable_tags(identity):return None
    place,setting,outfit,action,props=next((look[1:] for look in ROUTINE_LOOKS
        if any(word in activity for word in look[0])),DEFAULT_LOOK)
    labels={'location':place,'outfit':outfit}
    if state['clothing']!=defaults['clothing']:
        saved=cache.get('outfit',{})
        if saved.get('source')!=normalize(state['clothing']) or not usable_tags(saved.get('tags')):return None
        outfit=saved['tags']
        labels.pop('outfit')
    if state['location']!=defaults['location']:
        saved=cache.get('setting',{})
        if saved.get('source')!=normalize(state['location']) or not usable_tags(saved.get('tags')):return None
        setting=saved['tags']
        if re.search(r'拉伸|伸展|运动|练习',activity) and re.search(r'\b(?:home|room|floor)\b',setting,re.I):
            action,props='settling her balance at the end of a stretch, shoulders relaxing as she exhales',''
        elif re.search(r'\b(?:desk|table)\b',setting,re.I):
            action,props='resting one forearm on the tabletop and shifting her weight forward',''
        else:
            action,props='turning slightly as she settles into a relaxed stance',''
        labels.pop('location')
    brief=dict(identity=identity,outfit=outfit,setting=setting,action=action,composition='candid framing centered on the activity')
    creative=dict(concept='日常随手照',narrative_moment=activity or '在当前地点稍作停留',subject=identity,
        action=action,expression='relaxed expression',gesture=action,props=props or 'no additional props',
        environment=setting,atmosphere='quiet atmosphere',focal_point=action,emotional_intent='日常记录',
        context_connection='当前虚拟日程与已保存的角色设定',creative_reason='当前活动中的一个自然停顿',
        art_direction={'visual_focus':action,
                       'material_details':'subtle fabric folds following the movement of her shoulders'})
    return dict(state=dict(state),visual_scene=brief,visual_tags=', '.join(brief.values()),creative_brief=creative,
                image_plan={'kind':'life','action':action,'props':props,'mood':'relaxed expression'},send_image=True,
                _local_visual=True,_local_scene_labels=labels)


def apply_request_options(data,text):
    """Bind per-image overrides to the user's request before reusing old tags."""
    scene=data.get('visual_scene')
    scene=scene if isinstance(scene,dict) else {}
    proposed=data.get('visual_overrides')
    proposed=proposed if isinstance(proposed,dict) else {}
    data['_visual_overrides']=[field for field in ('identity','outfit','setting')
        if usable_tags(scene.get(field)) and isinstance(proposed.get(field),str)
        and len(proposed[field].strip())>=2 and proposed[field] in text]
    data.pop('_visual_hour',None)
    if 'setting' in data['_visual_overrides']:
        for terms,hour in ((r'深夜|夜景|夜晚|夜里|晚上',22),(r'傍晚|夕阳|黄昏|落日',18),
                           (r'清晨|黎明|日出',6),(r'白天|中午',12)):
            if re.search(terms,proposed['setting']):
                data['_visual_hour']=hour
                break
    exclusions=excluded_objects(text)
    proposed=data.get('visual_exclusions')
    for item in proposed[:6] if isinstance(proposed,list) else []:
        if not isinstance(item,dict):continue
        tag,evidence=item.get('tag'),item.get('evidence')
        if (isinstance(tag,str) and re.fullmatch(r'[a-zA-Z][a-zA-Z -]{1,35}',tag.strip())
                and isinstance(evidence,str) and evidence in text
                and re.search(r'别|不要|不用|不穿|不戴|去掉|without|no ',evidence,re.I)):
            exclusions.append(tag.strip().lower())
    data['_visual_exclusions']=list(dict.fromkeys(exclusions))[:8]


def usable_tags(value):
    return isinstance(value,str) and 0<len(value.strip())<=1200 and not re.search(r'[\u4e00-\u9fff]',value)


def complete_from_cache(session, data):
    """Reuse only vocabulary whose saved source still matches this exact scene."""
    brief = validate_brief(data.get('visual_scene'))
    cache = session.get('visual_cache', {})
    state = data['state']
    overrides=data.get('_visual_overrides',[])
    if not data.get('_local_visual') and cache.get('identity') == normalize(state['character']):
        if 'identity' not in overrides and usable_tags(cache.get('identity_tags')):
            brief['identity'] = cache['identity_tags']
        for field, source in (('outfit', 'clothing'), ('setting', 'location')):
            item = cache.get(field, {})
            if field not in overrides and (field=='outfit' or not usable_tags(brief.get(field))) and item.get('source') == normalize(state[source]) and usable_tags(item.get('tags')):
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
            parts.append((label if field=='outfit' else '上一张同一大地点的子场景，仅供参考，当前活动或新规划优先')+'：'+item['tags'])
    return '\n'.join(parts)


def assemble(session, data):
    state = data['state']
    brief = validate_brief(data.get('visual_scene'))
    if not all(usable_tags(brief.get(field)) for field in FIELDS):
        # Older model outputs remain usable; never cache a whole legacy prompt as clothing.
        return data['visual_tags']
    if data.get('_local_visual') or data.get('_visual_overrides'):
        return ', '.join(brief.values())
    cache = session.get('visual_cache', {})
    identity = normalize(state['character'])
    if cache.get('identity') != identity:
        cache = {'identity':identity, 'character_id':character_key(state['character'])}
    if not usable_tags(cache.get('identity_tags')):cache['identity_tags']=brief['identity']
    tags = [cache['identity_tags']]
    for field, source in [('outfit','clothing'), ('setting','location')]:
        item = cache.get(field, {})
        source_key = normalize(state[source])
        if item.get('source') != source_key or not usable_tags(item.get('tags')) or field=='setting' and item.get('tags') != brief[field]:
            item = {'source':source_key, 'tags':brief[field]}
            cache[field] = item
        tags.append(item['tags'])
    tags.extend([brief['action'], brief['composition']])
    session['visual_cache'] = cache
    return ', '.join(tags)
