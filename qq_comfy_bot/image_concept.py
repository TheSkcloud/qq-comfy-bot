"""Creative intent is validated independently of camera selection and stable scene anchors."""
import copy
import json
import re

FIELDS=('concept','narrative_moment','subject','action','expression','gesture','props','environment','atmosphere',
        'focal_point','emotional_intent','context_connection','creative_reason')


def validate(value):
    if not isinstance(value,dict):return {}
    if any(not isinstance(value.get(k),str) or not value[k].strip() or len(value[k])>1000 for k in FIELDS):return {}
    if any(re.search(r'[\u4e00-\u9fff]',value[k]) for k in ('subject','action','expression','gesture','props','environment','atmosphere','focal_point')):return {}
    return {k:value[k].strip() for k in FIELDS}


def needs_plan(data):
    return not validate(data.get('creative_brief')) or data.get('_thinking_enabled') is False


def normalize(value,scene=None,plan=None):
    """Recover optional visual detail from the same plan; never invent the creative reason."""
    if not isinstance(value,dict):return value
    result=copy.deepcopy(value);scene=scene if isinstance(scene,dict) else {};plan=plan if isinstance(plan,dict) else {}
    if any(not isinstance(result.get(k),str) or not result[k].strip() for k in ('concept','narrative_moment','context_connection','creative_reason')):return value
    fallback={'subject':scene.get('identity'),'action':scene.get('action'),'expression':plan.get('mood','natural expression'),
              'gesture':scene.get('action'),'props':plan.get('props','no additional props'),'environment':scene.get('setting'),
              'atmosphere':'natural local lighting','focal_point':scene.get('action')}
    for key,default in fallback.items():
        if isinstance(result.get(key),list) and all(isinstance(v,str) for v in result[key]):result[key]=', '.join(result[key])
        current=result.get(key)
        if not isinstance(current,str) or not current.strip() or re.search(r'[\u4e00-\u9fff]',current):
            if isinstance(default,str) and default.strip() and not re.search(r'[\u4e00-\u9fff]',default):result[key]=default
    if not isinstance(result.get('emotional_intent'),str) or not result['emotional_intent'].strip():result['emotional_intent']=result['creative_reason']
    return result


def instruction():
    return ('绘图时先构思具体的一幅画，再输出英文场景词汇。creative_brief:{'+','.join(FIELDS)+'}各字段为非空字符串；'
        'concept/narrative_moment/emotional_intent/context_connection/creative_reason用简短中文，说明此刻为什么画这个具体瞬间，不是模板安慰；'
        '先确定可见的瞬间与一个细小转折：Life抓住活动中的停顿或细节，Comfort给不越界的小画，Comic呼应有证据的共同梗；不要只是换表情的站桩肖像，也不强行加戏。转折要落实到action/gesture/props/focal_point，创意理由本身不是画面标签。优先一个清楚动作、少量道具，避免复杂双手接触。'
        'subject/action/expression/gesture/props/environment/atmosphere/focal_point用英文视觉描述，无道具写no additional props。'
        '用户明确的动作、衣服、地点、视角优先，当前角色身份/发型/服装/地点保持连续，时间以当前时钟为准。'
        'Life来自角色已建立的活动，Comfort/Comic可以想象但不改生活状态，不把用户经历搬到角色身上。'
        'creative_brief不选择camera/景别/画幅，后续本地shot_director负责怎么看。'
        '再输出visual_scene五字段作为固定身份/衣服/地点词汇和动作描述；不要输出推理过程。')


def apply(data):
    brief=validate(data.get('creative_brief'))
    if not brief:return False
    data['creative_brief']=brief
    # Camera never comes from the creative module. Stable visual anchors are retained.
    plan=data.get('image_plan') if isinstance(data.get('image_plan'),dict) else {}
    from visual_brief import usable_tags
    scene=dict(data.get('visual_scene') or {})
    # A concrete current environment is the single prompt and QA target.
    environment=brief['environment']
    if usable_tags(environment) and not re.search(r'\bsaved\b|\bcurrent location\b',environment,re.I):
        scene['setting']=environment
    action=brief['action']
    # Keep novel spatial refinements without repeating a summary of the same action.
    words=set(re.findall(r'[a-z]+',action.casefold()))
    for clause in brief['gesture'].split(','):
        tokens=set(re.findall(r'[a-z]+',clause.casefold()))
        if tokens and len(tokens & words)/len(tokens)<.65:
            action=unique_tags(action,clause);words.update(tokens)
    scene['action']=action
    data['visual_scene']=scene
    data['image_plan']={**plan,'action':action,
                        'props':brief['props'],'mood':brief['expression']}
    return True


def unique_tags(*values):
    result=[];seen=set()
    for value in values:
        for part in value.split(','):
            part=part.strip();key=re.sub(r'\s+',' ',part).casefold()
            if key and key not in seen:result.append(part);seen.add(key)
    return ', '.join(result)


def conflicts(data):
    """Reject obvious contradictory staging before a GPU submission."""
    scene=data.get('visual_scene',{})
    setting=scene.get('setting','').lower()
    action=data.get('image_plan',{}).get('action',scene.get('action','')).lower()
    issues=[]
    if re.search(r'\b(?:street|sidewalk|outdoors)\b',setting) and re.search(r'\b(?:indoor|indoors|bedroom|kitchen|home room)\b',setting):issues.append('indoor_outdoor_conflict')
    location=data.get('state',{}).get('location','')
    if re.search(r'家|房间|宿舍|寝室|\bhome\b|\broom\b',location,re.I) and re.search(r'^(?:residential )?street\b|\boutdoors\b|\bsidewalk\b',setting):issues.append('saved_location_conflict')
    if re.search(r'\b(?:standing|stand)\b',action) and re.search(r'\b(?:sitting|seated)\b',action):issues.append('standing_sitting_conflict')
    if re.search(r'家|宿舍|寝室|home',location,re.I) and not re.search(r'\bhome\b',setting):
        if re.search(r'\b(?:rehearsal room|dance studio|cafe|coffee shop|bubble tea shop|restaurant)\b',setting):issues.append('saved_location_conflict')
    return list(dict.fromkeys(issues))


def context(session,text,data):
    return json.dumps({'request':text,'state':session['state'],'life_state':session.get('life_state',{}),
        'discussion':session.get('dialogue_state',{}),'recent_dialogue':session.get('history',[])[-6:],
        'recent_artwork':[{'plan':x.get('plan',{}),'actual':x.get('actual',{})} for x in session.get('shot_history',[])[-4:]],
        'candidate':data.get('_candidate'),'visual_reference':data.get('_visual_request_context',''),
        'existing_creative_brief':validate(data.get('creative_brief'))},ensure_ascii=False)[-14000:]
