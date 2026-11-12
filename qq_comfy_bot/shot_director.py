"""Activity-aware camera selection; never changes character, outfit or life state."""
import copy
import re
import time

DIMENSIONS = {
    'size': ('extreme_wide', 'wide', 'full', 'medium', 'close', 'extreme_close', 'detail'),
    'elevation': ('eye_level', 'high', 'low', 'overhead', 'ground_level'),
    'direction': ('front', 'side', 'back', 'three_quarter', 'over_shoulder'),
    'composition': ('centered', 'thirds', 'off_center', 'negative_space', 'foreground', 'frame'),
    'narrative': ('observer', 'subjective', 'establishing', 'action'),
    'stylization': ('natural', 'dutch', 'perspective', 'comic_deformation'),
}


def _shot(name, size, elevation, direction, composition, narrative, position, occupancy, tags, activities=(), styles=('life','comfort','comic'), stylization='natural'):
    return dict(id=name, size=size, elevation=elevation, direction=direction,
                composition=composition, narrative=narrative, stylization=stylization,
                position=position, occupancy=occupancy, tags=tags, activities=activities, styles=styles)


SHOTS = [
    _shot('room_establishing','extreme_wide','eye_level','three_quarter','frame','establishing','right third',.12,'extreme wide establishing shot, viewed through doorway, subject small on right third, room framing',('room','read','desk','书','房间','桌')),
    _shot('environment','wide','high','three_quarter','negative_space','establishing','lower left',.22,'wide shot, high angle, environmental storytelling, subject at lower left, generous negative space'),
    _shot('full_side','full','eye_level','side','thirds','action','left third',.72,'full body shot, side view, rule of thirds, natural action moment'),
    _shot('full_back','full','ground_level','back','foreground','observer','right third',.65,'full body rear view, ground level camera, soft foreground occlusion',('walk','street','garden','散步','街','花','运动')),
    _shot('medium_side','medium','eye_level','side','off_center','observer','right third',.48,'medium shot, side profile, subject off center on right'),
    _shot('medium_front','medium','eye_level','front','centered','observer','center',.5,'medium shot, front view, centered composition'),
    _shot('over_shoulder','medium','high','over_shoulder','foreground','subjective','left foreground',.45,'over the shoulder view from behind, foreground shoulder, activity and props in focus',('read','book','desk','write','draw','cook','书','桌','写','画','做饭')),
    _shot('profile_close','close','eye_level','side','thirds','observer','left third',.7,'close up side profile, subject on left third, selective background detail'),
    _shot('three_quarter_close','close','low','three_quarter','negative_space','observer','right third',.72,'close up, low angle, three quarter view, negative space on left'),
    _shot('face_detail','extreme_close','eye_level','three_quarter','off_center','observer','left',.9,'extreme close up of face, three quarter view, off center',styles=('comfort','comic')),
    _shot('hands_detail','detail','overhead','front','thirds','action','lower third',.25,'detail close up of hands and activity props, overhead view, face outside frame',('read','book','desk','write','draw','cook','cup','书','桌','写','画','做饭','杯')),
    _shot('table_level','medium','low','three_quarter','foreground','observer','right third',.5,'medium shot from tabletop height, low angle, foreground activity props',('read','desk','cup','eat','书','桌','杯','吃')),
    _shot('back_close','close','eye_level','back','frame','observer','center',.65,'close rear view, face not visible, framing by surrounding environment',('read','window','walk','书','窗','散步')),
    _shot('comic_tilt','medium','low','three_quarter','off_center','action','diagonal right',.6,'dutch angle, playful diagonal composition, exaggerated action',styles=('comic',),stylization='dutch'),
    _shot('comic_perspective','full','ground_level','front','foreground','action','center',.75,'full body, ground level, exaggerated foreshortening, dynamic comic perspective',styles=('comic',),stylization='perspective'),
    _shot('comic_reaction','close','high','front','centered','action','center',.8,'close up comic reaction, high angle, playful comic deformation',styles=('comic',),stylization='comic_deformation'),
]

PATTERNS = {
    'size': [('extreme_wide',r'大远景|extreme wide'),('wide',r'远景|wide shot|long shot'),('full',r'全身|全景|full.body'),('medium',r'半身|中景|medium shot|upper body'),('detail',r'手部|局部|细节特写|hand.{0,8}close|detail close'),('extreme_close',r'大特写|extreme close'),('close',r'特写|近景|close.up')],
    'elevation': [('overhead',r'顶视|正上方|overhead|bird.s.eye'),('high',r'俯视|高机位|high angle'),('ground_level',r'地面机位|ground.level'),('low',r'仰视|低机位|low angle'),('eye_level',r'平视|eye.level')],
    'direction': [('over_shoulder',r'越肩|过肩|over.the.shoulder'),('back',r'背影|背面|从背后|rear view|back view'),('three_quarter',r'四分之三|三分之四|three.quarter'),('side',r'侧脸|侧面|side view|side profile'),('front',r'正脸|正面|front view')],
    'composition': [('thirds',r'三分法|rule of thirds'),('off_center',r'偏置|off.center'),('centered',r'居中|centered'),('negative_space',r'留白|negative space'),('foreground',r'前景遮挡|foreground occlusion'),('frame',r'框架构图|门口|doorway')],
}


def classify(text):
    text=str(text).lower()
    result={}
    for dimension, patterns in PATTERNS.items():
        for value, pattern in patterns:
            if re.search(pattern,text):
                result[dimension]=value
                break
    return result


def _expectations(plan):
    partial=plan['size']=='detail'
    distant=plan['size'] in ('wide','extreme_wide')
    face=not partial and not distant and plan['direction'] not in ('back','over_shoulder')
    return dict(face_expected=face, eyes_expected=face, full_body_expected=plan['size']=='full',
                subject='hands and activity props' if partial else 'character within environment' if distant else 'character performing planned action',
                identity_scope='visible features only; absence of off-frame face/eyes is expected' if not face else 'visible hair, eyes and face',
                required_checks=['visible identity conflicts','visible outfit','planned subject/action','setting/time','visible anatomy'])


def choose(snapshot, request='', history=()):
    """Score suitable shots against the last twelve actual/planned compositions."""
    style=snapshot.get('style','life')
    activity=' '.join(str(snapshot.get(k,'')) for k in ('action','props','location','activity')).lower()
    creative=snapshot.get('creative_brief',{})
    activity+=' '+str(creative.get('narrative_moment',''))+' '+str(creative.get('focal_point',''))
    locked=classify(request)
    avatar=snapshot.get('purpose')=='avatar'
    fixed=bool(re.search(r'固定镜头|同一.{0,3}(?:角度|镜头)|保持.{0,3}(?:角度|构图)|fixed (?:camera|shot)',request,re.I))
    if avatar:locked.update(size='close',direction='front',composition='centered')
    suitable=[s for s in SHOTS if style in s['styles'] and (not s['activities'] or any(w in activity for w in s['activities']))]
    if snapshot.get('purpose')=='poster' and 'size' not in locked:
        suitable=[s for s in suitable if s['size'] in ('full','medium','close')]
    recent=list(history)[-12:]
    recent_hand_failure=any(item.get('plan',{}).get('size')=='detail' and item.get('anatomy_ok') is False
        and 0<=time.time()-item.get('at',0)<86400
        and (not item.get('character_id') or not snapshot.get('character_id') or item['character_id']==snapshot['character_id']) for item in recent[-4:])
    avoid_failed_hands=recent_hand_failure and 'size' not in locked and not fixed
    def score(shot):
        points=sum(100 for k,v in locked.items() if shot[k]==v)
        points+=3 if shot['activities'] and any(w in activity for w in shot['activities']) else 0
        focus=str(creative.get('focal_point','')).lower()
        fine_action=bool(re.search(r'fingertip|page corner|pen tip|paper edge|handwriting|grip',focus))
        face_focus=bool(re.search(r'face|gaze|eyes|expression|glanc|smirk|smile|eyebrow|chin',focus))
        hand_focus=bool(re.search(r'hand|finger',focus)) and (fine_action or not face_focus)
        if hand_focus and shot['size']=='detail':points+=6
        if avoid_failed_hands and shot['size']=='detail':points-=1000
        if face_focus and not fine_action and shot['size']=='detail':points-=8
        if face_focus and shot['direction'] not in ('back','over_shoulder') and shot['size'] in ('close','extreme_close'):points+=6
        if re.search(r'entire room|environment|window light',focus) and shot['size'] in ('wide','extreme_wide'):points+=4
        if re.search(r'dance|balance|body motion|skirt movement',focus) and shot['size']=='full':points+=5
        if style=='comfort' and shot['size'] in ('medium','close'):points+=4
        if style=='comic' and shot['stylization']!='natural':points+=4
        for index, item in enumerate(reversed(recent)):
            actual=item.get('actual',{})
            old={**item.get('plan',{}),**{k:v for k,v in actual.items() if k in DIMENSIONS and v in DIMENSIONS[k]}}
            weight=(12-index)/12
            points-=weight*sum(w for k,w in (('size',7),('direction',6),('composition',4),('elevation',2)) if old.get(k)==shot[k])
            if old.get('id')==shot['id']:points-=12*weight
        return points
    selected=copy.deepcopy(max(suitable,key=score))
    selected.update(locked)
    selected.pop('activities');selected.pop('styles')
    selected['user_locked']=locked
    selected['reason']='user camera constraints' if locked or fixed or avatar else 'activity/style suitability, then recent twelve compositions'
    if avoid_failed_hands:selected.update(avoided_recent_hand_failure=True,reason='avoid recent hand anatomy failure; retain user camera overrides')
    if creative:selected['creative_focal_point']=creative.get('focal_point','')
    # A lock is a complete override; do not retain contradictory template camera words.
    if locked:
        vocabulary={'size':{'extreme_wide':'extreme wide shot','wide':'wide shot','full':'full body shot','medium':'medium shot','close':'close up','extreme_close':'extreme close up','detail':'detail close up of hands and activity props, face outside frame'},
                    'direction':{'front':'front view','side':'side view','back':'rear view, face not visible','three_quarter':'three quarter view','over_shoulder':'over the shoulder view from behind'},
                    'elevation':{'eye_level':'eye level','high':'high angle','low':'low angle','overhead':'overhead view','ground_level':'ground level'},
                    'composition':{'centered':'centered composition','thirds':'rule of thirds','off_center':'off center composition','negative_space':'negative space','foreground':'foreground occlusion','frame':'frame within frame'}}
        selected['tags']=', '.join(vocabulary[k][selected[k]] for k in vocabulary)
        selected['position']='center' if selected['composition']=='centered' else 'off center'
        selected['occupancy']={'extreme_wide':.12,'wide':.22,'full':.72,'medium':.5,'close':.75,'extreme_close':.9,'detail':.25}[selected['size']]
    if (fixed and not locked) or avatar:
        selected.update(classify(snapshot.get('camera','')))
        selected['tags']=snapshot['camera']
    selected['expectations']=_expectations(selected)
    selected['action']=snapshot.get('action','')
    selected['foreground']='activity props' if selected['composition']=='foreground' else ''
    selected['background']=snapshot.get('setting_tags','')
    selected['props']=snapshot.get('props','')
    selected['orientation']='landscape' if selected['size'] in ('wide','extreme_wide') or selected['direction']=='over_shoulder' else 'square' if selected['size']=='detail' else 'portrait'
    if re.search(r'横版|横图|横向|landscape',request,re.I):selected['orientation']='landscape'
    elif re.search(r'竖版|竖图|竖向|portrait format',request,re.I):selected['orientation']='portrait'
    elif avatar or re.search(r'方图|正方形|square',request,re.I):selected['orientation']='square'
    return selected


def history_entry(snapshot, qa, render, task_id):
    observed=qa.get('composition',{})
    actual={k:v for k,v in observed.items() if k in DIMENSIONS and v in DIMENSIONS[k]} if isinstance(observed,dict) else {}
    return dict(image_task_id=task_id, plan=copy.deepcopy(snapshot.get('shot_plan',{})), actual=actual,
                at=time.time(),character_id=snapshot.get('character_id'),quality_pass=qa.get('pass') is True,anatomy_ok=qa.get('anatomy_ok'),
                observation_source='vision' if actual else 'unavailable', visible=qa.get('visible',''),
                aspect_ratio=[render.get('width'),render.get('height')])


def visible_identity(snapshot):
    """Do not ask the generator for facial details outside the planned frame."""
    plan=snapshot.get('shot_plan',{})
    if plan.get('size')=='detail':return "adult woman's hands"
    identity=snapshot['identity']
    if not plan.get('expectations',{}).get('eyes_expected',True):
        identity=', '.join(tag.strip() for tag in identity.split(',') if not re.search(r'\beyes?\b|\bgaze\b',tag,re.I))
    return identity


def guide_prompt(snapshot, prompt):
    plan=snapshot.get('shot_plan')
    if not plan:return prompt
    size_tags={'extreme_wide':'(very wide shot:1.4), (small figure in a large room:1.35), distant camera, entire room visible',
               'wide':'(wide shot:1.35), distant camera, small figure, environment in focus',
               'full':'(full body:1.25), head to toe visible',
               'medium':'medium shot', 'close':'(close-up:1.25)',
               'extreme_close':'(extreme close-up:1.3)',
               'detail':'(hands focus:1.4), (close-up of hands:1.35), only hands and activity props in frame, tabletop fills the image, face out of frame'}
    direction={'back':'(from behind:1.4), back view, looking away',
               'over_shoulder':'(over the shoulder:1.35), from behind, looking at the book',
               'side':'(from side:1.3), side profile',
               'three_quarter':'three quarter view','front':'front view'}
    facing='top down view of the activity surface' if plan['size']=='detail' else direction[plan['direction']]
    return ', '.join([size_tags[plan['size']],facing,plan['tags'],visible_action(snapshot),prompt])


def negative_tags(plan):
    tags=[]
    if plan.get('size') in ('wide','extreme_wide'):tags+=['close-up','portrait','upper body','large foreground character']
    if plan.get('direction') in ('back','over_shoulder'):tags+=['front view','looking at viewer','eye contact']
    if plan.get('size')=='detail':tags+=['portrait','face','head','full body','upper body']
    return ', '.join(tags)


def visible_action(snapshot):
    action=snapshot.get('action','')
    if snapshot.get('shot_plan',{}).get('size')=='detail':
        action=re.sub(r'(?:sitting|standing|seated)[^,]*,?\s*','',action,flags=re.I)
        action=visible_focus(snapshot,action)
    return action


def visible_outfit(snapshot):
    outfit=snapshot['outfit_tags']
    if snapshot.get('shot_plan',{}).get('size')=='detail':
        outfit=', '.join(t.strip() for t in outfit.split(',') if not re.search(r'\b(?:skirt|pants|trousers|shorts|shoes|boots|stockings|slippers|socks)\b',t,re.I))
        outfit+=' sleeves visible at wrists'
    return outfit


def visible_emotion(snapshot):
    return snapshot.get('emotion','') if snapshot.get('shot_plan',{}).get('expectations',{}).get('face_expected',True) else ''

def visible_focus(snapshot,value=None):
    value=value if value is not None else snapshot.get('creative_brief',{}).get('focal_point','')
    if snapshot.get('shot_plan',{}).get('size')!='detail':return value
    parts=re.split(r',|\band\b',value,flags=re.I)
    return ', '.join(part.strip() for part in parts if part.strip() and not re.search(r'\b(?:face|eyes?|gaze|head|smile|smirk|looking|eyebrows?|lips)\b',part,re.I))
