"""Grounded candidates, independently produced before spending a rendering-plan request."""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import hashlib, json, uuid
import dialogue
from companion import character_key

@dataclass
class ImageCandidate:
    candidate_id:str
    source_type:str
    trigger_reason:str
    related_event_id:str
    conversation_id:str
    scene_description:dict
    character_activity:str
    location:str
    outfit:str
    emotional_context:str
    priority:int
    created_at:str
    expires_at:str
    dedupe_key:str
    trigger_actor:str="life"
    trigger_evidence:str=""
    image_value:str=""

    def data(self):return asdict(self)


def make(kind,key,event,reason,activity,session,current,emotion=''):
    digest=hashlib.sha256(json.dumps([kind,event,character_key(session['state']['character'])],ensure_ascii=False).encode()).hexdigest()
    return ImageCandidate(uuid.uuid4().hex,kind,reason,event,hashlib.sha256(key.encode()).hexdigest()[:12],
        dict(session['state']),activity,session['state']['location'],session['state']['clothing'],emotion,
        {'explicit':0,'comfort':10,'comic':20,'life':30}[kind],current.isoformat(),
        (current+timedelta(minutes=15)).isoformat(),digest)


def life(key,session,current,routine_event):
    activity='';event=''
    live=session.get('life_state',{})
    try:
        if datetime.fromisoformat(live['expires_at'])>current and live.get('character')==character_key(session['state']['character']):
            activity=live['activity'];event=live['started_at']+':'+activity
    except (ValueError,KeyError,TypeError):pass
    if not activity and routine_event:
        activity=routine_event['activity'];event=routine_event['event_id']
    # Sleep or vague administrative schedule slots are not sharing events.
    if not activity or any(w in activity for w in ('睡觉','睡前','准备休息','洗漱','起床','处理手边','安排今天')):
        return None
    return make('life',key,event,'current_fictional_activity',activity,session,current)


def conversation(key,session,text,data,current):
    import social_reasoning
    if social_reasoning.boundary_blocks(session,text,current):return None
    social=social_reasoning.normalize(data,text,session.get('history',[]))
    value=social['image_value']
    if value.get('veto'):return None
    emotion=social['emotion_state']
    event=social['interaction_event']
    repeated=sum(x.get('category')==emotion['category'] for x in session.get('social_state',[])[-3:])>=2
    comic_opportunity=(social['trigger_actor'] in ('user','shared') and bool(social['trigger_evidence']) and event['type'] in {'mimicry','shared_joke'})
    comfort_opportunity=(emotion['evidence'] and emotion['category'] in {'disappointment','frustration','unfairness','self_deprecation','sadness'} and (repeated or emotion['intensity']>=.65 or event['type']=='setback'))
    if not value['suitable']:
        if comic_opportunity:value={'suitable':True,'kind':'comic','reason':'用一张小漫画回应已有模仿或共同笑点'}
        elif comfort_opportunity:value={'suitable':True,'kind':'comfort','reason':'用小画回应持续或具体的挫折'}
        else:return None
    if value['kind']=='comfort' and emotion['category'] in {'disappointment','frustration','unfairness','self_deprecation','sadness'} and emotion['evidence'] and (emotion['intensity']>=.45 or comfort_opportunity):
        kind='comfort';reason='grounded_emotion_with_image_value'
    elif value['kind']=='comic' and social['trigger_actor'] in ('user','shared') and social['trigger_evidence'] and event['type'] in {'mimicry','shared_joke','self_deprecation'} and social['user_intent'] in {'teasing','emotion'}:
        kind='comic';reason='user_interaction_with_image_value'
    else:return None
    # Consecutive turns about one topic share an event instead of drawing for every rewording.
    previous=session.get('interaction_image_event',{})
    event=text
    try:
        if previous.get('kind')==kind and (current-datetime.fromisoformat(previous['at'])).total_seconds()<1800:
            event=previous['event']
    except (ValueError,KeyError,TypeError):pass
    session['interaction_image_event']={'kind':kind,'event':event,'at':current.isoformat()}
    candidate=make(kind,key,event,reason,text,session,current,emotion['category'] if kind=='comfort' else '双方互动；想象漫画')
    candidate.trigger_actor=social['trigger_actor']
    candidate.trigger_evidence=social['trigger_evidence'] or emotion['evidence']
    candidate.image_value=value['reason']
    return candidate
