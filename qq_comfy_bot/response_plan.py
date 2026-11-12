"""One-pass reply decisions; compatible with old reply-only model outputs."""
INTENTS = {'casual_chat','acknowledge','share','advice','question','teasing','emotion','closing','image_request'}
REACTIONS = {'none','happy','think','cheer','shy','sigh'}


def decode(content, force_image=False):
    """Recover plain chat text only. Never expose malformed internal plans as QQ text."""
    import json,re
    content=content.strip().removeprefix('\ufeff')
    if content.startswith('```') and content.endswith('```'):
        content=re.sub(r'^```(?:json)?\s*|\s*```$','',content,flags=re.I).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        try:
            first,end=json.JSONDecoder().raw_decode(content)
            tail=content[end:].strip()
            if isinstance(first,dict) and tail and re.fullmatch(r'[}\]\s]+',tail):
                return {**first,'_model_format':'trailing_delimiter_recovered'}
        except json.JSONDecodeError:pass
        if force_image:
            raise
        recovered=partial_text(content)
        if recovered is not None:return recovered
        text=content.strip()
        text=re.sub(r'^\[(?:\d{2}-\d{2}\s+\d{1,2}时|历史)\]\s*','',text)
        if (not 0<len(text)<=1200 or text.startswith(('{','[','"','```'))
                or re.search(r'["\']?(?:reply|state|response_plan|visual_tags|visual_scene|send_image|memory_updates|affect|image_plan|waiting_lines|life_update|media_analysis|scene_update)["\']?\s*:',text)):
            raise
        return {'reply':text,'state':{},'send_image':False,'visual_tags':'',
                '_model_format':'plain_text_recovered'}


def normalize(data, state, user, force_image=False,allow_local_memory=True):
    """Repair optional bookkeeping locally; never spend a request to fix absent metadata."""
    import re
    from visual_brief import validate_brief
    if not isinstance(data,dict):
        raise ValueError('Model JSON must be an object')
    data=dict(data)
    reaction=data.get('reaction','none')
    data['reaction']=reaction if isinstance(reaction,str) and reaction in REACTIONS else 'none'
    try:
        plan=parse(data)
    except ValueError:
        data.pop('response_plan',None)
        plan=parse(data)
    from dialogue import interaction
    social=interaction(user,[])
    if social['intent'] and plan['intent']=='casual_chat':
        plan['intent']=social['intent']
    supplied=data.get('response_plan') if isinstance(data.get('response_plan'),dict) else {}
    if 'length' not in supplied:
        import speech
        mode=speech.plan(user,[])['mode']
        if mode=='help':plan['length']='extended' if len(user)>20 else 'normal'
        elif mode in ('ack','exit'):plan['length']='minimal'
    if plan['length']=='extended' and 'segment_priority' not in supplied:plan['segment_priority']=['essential']*len(plan['segments'])
    data['response_plan']=plan
    if force_image:data['response_plan']['intent']='image_request'
    data['reply']='\n'.join(plan['segments'])
    candidate=data.get('state')
    candidate=candidate if isinstance(candidate,dict) else {}
    merged={k:candidate.get(k) if isinstance(candidate.get(k),str) and 0<len(candidate[k])<=2000 else state[k]
            for k in ('character','clothing','location')}
    if not re.search(r'(?:扮演|角色.{0,4}(?:改成|换成)|换个角色|人设.{0,4}(?:改成|换成))',user):
        merged['character']=state['character']
    data['state']=merged
    tags=data.get('visual_tags','')
    data['visual_tags']=tags if isinstance(tags,str) and len(tags)<=6000 else ''
    try:
        data['visual_scene']=validate_brief(data.get('visual_scene'))
    except ValueError:
        data['visual_scene']={}
    if not data['visual_tags'].strip() and all(data['visual_scene'].get(k) for k in ('identity','outfit','setting','action','composition')):
        data['visual_tags']=', '.join(data['visual_scene'].values())
    data['send_image']=data.get('send_image') is True
    if not data['visual_tags'].strip():
        data['send_image']=False
    waiting=data.get('waiting_lines',[])
    data['waiting_lines']=list(dict.fromkeys(line.strip() for line in waiting if isinstance(line,str) and 0<len(line.strip())<=65))[:3] if isinstance(waiting,list) else []
    updates=data.get('memory_updates',[])
    data['_memory_extraction']={'status':'missing' if 'memory_updates' not in data else 'evaluated' if isinstance(updates,list) else 'invalid_type',
        'proposed':len(updates) if isinstance(updates,list) else 0,
        'normalization_rejected':sum(not isinstance(item,dict) for item in updates)+max(0,sum(isinstance(item,dict) for item in updates)-4) if isinstance(updates,list) else 0}
    data['memory_updates']=[item for item in updates if isinstance(item,dict)][:4] if isinstance(updates,list) else []
    if social['intent']=='teasing':
        data['_memory_extraction'].update(status='teasing_filtered',normalization_rejected=data['_memory_extraction']['proposed'])
        data['memory_updates']=[]  # Quoted/mimicked speech is not a reliable user fact.
    if (allow_local_memory and not force_image and social['intent']!='teasing' and not data['memory_updates']
            and data['_memory_extraction']['status'] in ('missing','evaluated') and not data['_memory_extraction']['normalization_rejected']
            and not data.get('_text_only_recovery') and not data.get('_model_format')):
        quote=user.strip().rstrip('。！!')
        if (re.fullmatch(r'我(?:更|一直|最)?(?:喜欢|不喜欢|讨厌|偏好)[^？?\n]{1,145}',quote)
                and not re.search(r'如果|假如|假设|开玩笑|模仿|反话|据说|骗|假的|不是说|[“”「」"\']|(?:吗|么)$',quote)):
            data['memory_updates']=[{'kind':'preference','value':quote,'evidence':quote}]
            data['_memory_extraction'].update(local_fallback='verbatim_preference',local_proposed=1)
    data['affect']=data.get('affect') if isinstance(data.get('affect'),dict) else {}
    return data


def parse(data):
    from message_style import decorate
    value = data.get('response_plan')
    if value is None:
        reply = data.get('reply','')
        if not isinstance(reply,str) or not 0<len(reply.strip())<=4000:
            raise ValueError('Legacy text reply is empty')
        return decorate({'intent':'casual_chat','mode':'text','should_reply':True,'segments':[reply]})
    if not isinstance(value,dict):
        raise ValueError('response_plan must be an object')
    intent = value.get('intent','casual_chat')
    mode = value.get('mode','text')
    should = value.get('should_reply', True)
    segments = value.get('segments', [])
    if not isinstance(intent,str) or intent not in INTENTS or not isinstance(mode,str) or mode not in ('text','reaction','silent') or type(should) is not bool:
        raise ValueError('Invalid response plan')
    if not isinstance(segments,list) or len(segments)>4 or not all(isinstance(s,str) and s.strip() for s in segments):
        raise ValueError('Invalid reply segments')
    if not should or mode=='silent':
        return decorate({'intent':intent,'mode':'silent','should_reply':False,'segments':[]},value)
    if mode=='text' and not segments:
        segments = [data.get('reply','')]
    if mode=='text' and not all(isinstance(s,str) and s.strip() for s in segments):
        raise ValueError('Text reply is empty')
    if sum(map(len,segments))>4000:
        raise ValueError('Reply too long')
    if mode=='reaction' and data.get('reaction','none') not in REACTIONS-{'none'}:
        raise ValueError('Reaction-only reply needs a reaction')
    return decorate({'intent':intent,'mode':mode,'should_reply':True,'segments':segments if mode=='text' else []},value)


def life_context(session, current):
    from datetime import datetime
    from companion import character_key
    value = session.get('life_state',{})
    if value.get('character') != character_key(session['state']['character']):
        return ''
    try:
        expiry = datetime.fromisoformat(value['expires_at'])
        if expiry <= current:
            return '先前的虚拟活动已过有效期，不继续断言仍在进行；当前日程只是新的计划背景。'
    except (KeyError,ValueError,TypeError):
        return ''
    return f"已保存的虚拟生活状态：{value['activity']}；开始于{value['started_at']}，有效至{value['expires_at']}。不临时编造另一项冲突活动，计划不等于已完成。"


def update_life(session, data, current):
    from datetime import timedelta
    from companion import character_key
    value = data.get('life_update')
    if not isinstance(value,dict) or not value:
        return
    activity = value.get('activity')
    minutes = value.get('minutes',30)
    if not isinstance(activity,str) or not 1<=len(activity)<=100 or type(minutes) is not int or not 5<=minutes<=120:
        return
    previous = session.get('life_state',{})
    identity = character_key(session['state']['character'])
    if previous.get('activity') == activity and previous.get('character') == identity:
        from datetime import datetime
        try:
            if datetime.fromisoformat(previous['expires_at'])>current:
                return  # Repeating a sentence must not renew an active activity indefinitely.
        except (KeyError,ValueError,TypeError):
            pass
    session['life_state'] = {'character':identity,'activity':activity,'started_at':current.isoformat(timespec='seconds'),
                             'expires_at':(current+timedelta(minutes=minutes)).isoformat(timespec='seconds'),'source':'fictional'}


def partial_text(content):
    """Recover complete top-level text fields only; incomplete plans cannot change state or draw."""
    import json
    text=content.strip()
    if not text.startswith('{') or len(text)>20000:return None
    decoder=json.JSONDecoder();pos=1;fields={}
    try:
        while pos<len(text):
            while pos<len(text) and text[pos].isspace():pos+=1
            key,pos=decoder.raw_decode(text,pos)
            if not isinstance(key,str):break
            while pos<len(text) and text[pos].isspace():pos+=1
            if pos>=len(text) or text[pos]!=':':break
            pos+=1
            while pos<len(text) and text[pos].isspace():pos+=1
            value,pos=decoder.raw_decode(text,pos);fields[key]=value
            while pos<len(text) and text[pos].isspace():pos+=1
            if pos>=len(text) or text[pos]!=',':break
            pos+=1
    except (ValueError,TypeError):pass
    reply=fields.get('reply')
    if not isinstance(reply,str) or not 0<len(reply.strip())<=4000:return None
    return {'reply':reply,'state':{},'send_image':False,'visual_tags':'','_model_format':'partial_text_recovered','_text_only_recovery':True}
