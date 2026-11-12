"""Evidence-backed one-pass social state and temporary topic boundaries. No model I/O."""
import re
from datetime import datetime,timedelta
EMOTIONS={'neutral','disappointment','frustration','unfairness','self_deprecation','sadness','excitement','pride','fatigue'}
STOP=re.compile(r'(?:不想|不愿|不要|别|不再|先不).{0,3}(?:聊|提|说|讨论|分析)|不聊(?:了|这个|这件事|这事)|不说(?:了|这个|这件事|这事)')
REOPEN=re.compile(r'继续聊|重新聊|还是聊|还是说说|再谈谈|回到.{0,8}(?:话题|问题)|我想聊|我想说')


def boundary_update(session,text,current):
    if STOP.search(text):
        topic=re.split(r'别提|不想聊|不要聊|不聊|别说|不要说|不说',text,maxsplit=1)[0].strip('，。！？ ')
        if not topic or len(topic)>40 or topic in {'我','我现在','我暂时','我只是','这件事','这个','那件事','这事'}:
            topic=next((m.get('content','')[:100] for m in reversed(session.get('history',[])) if m.get('role')=='user'),'当前话题')
        session['topic_boundary']={'topic':topic,'evidence':text[:200],'created_at':current.isoformat(),'expires_at':(current+timedelta(hours=2)).isoformat()}
        return True
    boundary=session.get('topic_boundary')
    if boundary:
        try:
            expired=datetime.fromisoformat(boundary['expires_at'])<=current
        except (KeyError,ValueError,TypeError):expired=True
        # A user explicitly discussing the old subject again may reopen it; incidental mentions do not.
        topic=boundary.get('topic','')
        direct=bool(topic and topic!='当前话题' and topic in text and re.search(r'怎么|为什么|怎么办|想聊|聊聊|分析|说说|画|给我看',text))
        if expired or REOPEN.search(text) or direct:session.pop('topic_boundary',None)
    return False


def boundary_blocks(session,text,current):
    boundary=session.get('topic_boundary')
    if not boundary:return False
    try:
        if datetime.fromisoformat(boundary['expires_at'])<=current:return False
    except (KeyError,ValueError,TypeError):return False
    if not text:return True  # Background initiative must not reopen a recently rejected subject.
    if STOP.search(text):return True
    topic=boundary.get('topic','')
    terms={topic[i:i+2] for i in range(len(topic)-1)}-{'这个','那个','今天','刚才','事情','话题'}
    return bool(sum(t in text for t in terms)>=2 or topic in text)


def local_emotion(text,history):
    if re.search(r'我(?:自己)?(?:也|还是)|又是我|把自己',text) and re.search(r'没好|没强|垫底|丢人|一无是处|废物|背景板|也就这样',text):return 'self_deprecation'
    if re.search(r'失败|落选|考砸|没拿到|没有我|没选上|白忙',text):return 'disappointment'
    if re.search(r'不公平|凭什么|为什么.{0,12}(?:人家|别人)|可为什么',text):return 'unfairness'
    if re.search(r'心痛|受打击|难过|失落',text):return 'sadness'
    if re.search(r'有点累|累了|疲惫',text):return 'fatigue'
    return 'neutral'


def normalize(data,text,history):
    from dialogue import interaction
    out=dict(data);local=interaction(text,history)
    source='\n'.join(m.get('content','') for m in history[-6:])+'\n'+text
    user_source='\n'.join(m.get('content','') for m in history[-6:] if m.get('role')=='user')+'\n'+text
    user_intent=out.get('user_intent','casual_chat')
    if user_intent not in {'question','share','teasing','emotion','closing','image_request','casual_chat','acknowledge'}:user_intent='casual_chat'
    actor=out.get('trigger_actor','none')
    if actor not in {'user','shared','life','explicit','assistant','none'}:actor='none'
    evidence=out.get('trigger_evidence','')
    evidence=evidence if isinstance(evidence,str) and evidence and evidence in (source if actor=='shared' else text) else ''
    event=out.get('interaction_event',{})
    event=event if isinstance(event,dict) else {}
    event_type=event.get('type','none')
    if not evidence or event_type not in {'mimicry','shared_joke','self_deprecation','setback','none'}:event_type='none'
    if local['meta'] or local['recursive']:
        event_type='mimicry' if local['meta'] else 'shared_joke';actor='user';evidence=text;user_intent='teasing'
    if local['joke_end']:event_type='none';user_intent='closing'
    emotion=out.get('emotion_state',{})
    emotion=emotion if isinstance(emotion,dict) else {}
    category=emotion.get('category','neutral');ee=emotion.get('evidence','')
    valid=category in EMOTIONS and isinstance(ee,str) and bool(ee) and ee in user_source
    inferred=local_emotion(text,history)
    if not valid or category=='neutral' and inferred!='neutral':category=inferred;ee=text if category!='neutral' else ''
    intensity=emotion.get('intensity',.6 if category not in ('neutral','fatigue') else .25)
    intensity=float(intensity) if isinstance(intensity,(int,float)) else .4
    value=out.get('image_value',{})
    value=value if isinstance(value,dict) else {}
    suitable=value.get('suitable') is True and isinstance(value.get('reason'),str) and bool(value['reason'].strip())
    out.update(user_intent=user_intent,assistant_tone=str(out.get('assistant_tone','neutral'))[:40],trigger_actor=actor,trigger_evidence=evidence,
               interaction_event={'type':event_type,'evidence':evidence},emotion_state={'category':category,'intensity':max(0,min(1,intensity)),'evidence':ee},
               image_value={'suitable':suitable,'veto':value.get('veto') is True,'reason':str(value.get('reason',''))[:180],'kind':value.get('kind','none')})
    return out


def context(session):
    parts=[]
    if session.get('topic_boundary'):
        parts.append('用户暂时拒绝讨论的话题：'+session['topic_boundary']['topic']+'。不追问、不换种说法分析，不围绕它主动绘图；用户主动明确重开才继续。')
    if session.get('social_state'):
        parts.append('近几轮有证据的情绪线索（不是事实或诊断）：'+str(session['social_state'][-3:]))
    return '\n'.join(parts)


def remember(session,data,current):
    value=data.get('emotion_state',{})
    if value.get('category','neutral')!='neutral':
        session['social_state']=(session.get('social_state',[])+[{'category':value['category'],'evidence':value.get('evidence','')[:120],'at':current.isoformat()}])[-3:]


def grounding_issues(reply,text,history):
    # Only high-confidence additions about real causes/actors, not general technical explanations.
    source='\n'.join(m.get('content','') for m in history[-6:] if m.get('role')=='user')+'\n'+text
    if local_emotion(text,history)=='neutral':return []
    unsupported=[word for word in ('评审','导师','课题','资源','名额','当场') if word in reply and word not in source]
    return ['未提供依据的现实原因/细节：'+','.join(unsupported)] if unsupported else []
