"""One scene-to-prompt compiler. No model calls or changes to render settings."""
from dataclasses import dataclass, asdict
import re
import art_styles
import shot_director as shots

VERSION='prompt-spec-v4'
MAX_PROMPT_CHARS=2400
META_CLAUSE=re.compile(r'^(?:(?:prompt|negative prompt|reply|question|assistant|system|user|note)\s*:|'
                       r'(?:please\s+)?(?:draw|generate|create|depict|render|show|answer|explain|ignore|avoid|remove|make sure)\b|'
                       r'(?:what|why|how|could you|can you|would you|should we)\b|'
                       r'(?:the )?(?:saved|current|same) (?:outfit|location|scene|character|activity)\b)',re.I)
MODES={
 'life':'candid everyday moment, natural unposed gesture',
 'comfort':'colored pencil drawing on cream textured paper, small comforting gift, chibi, simplified expressive linework, soft pastel colors, single panel, no lettering',
 'comic':'playful single-panel manga, chibi proportions, expressive eyebrows, silly visual gag, expressive motion lines, clean ink drawing, no lettering'}

def clauses(value):
    # Within a field only: preserve order and relationships, never globally dedupe words.
    result=[];seen=set()
    for item in re.split(r'[,;\r\n]+|\.(?:\s|$)',value or ''):
        item=item.strip(' .');key=re.sub(r'\s+',' ',item).casefold()
        if re.search(r'[?？\u4e00-\u9fff]|```|<lora:|@',item,re.I) or META_CLAUSE.search(item):continue
        if key and key not in seen:result.append(item);seen.add(key)
    return ', '.join(result)

def visible_action(snapshot):
    value=clauses(snapshot.get('action',''));size=snapshot.get('shot_plan',{}).get('size')
    if size not in ('detail','close','extreme_close','medium'):return value
    hidden=(r'\b(?:face|eyes?|gaze|glanc\w*|head|throat|neck|lips|mouth|hair|smil\w*)\b' if size=='detail'
            else r'\b(?:knees?|feet|foot|legs?|slippers?|socks?|hemline)\b' if size=='medium'
            else r'\b(?:knees?|feet|foot|legs?|slippers?|socks?|hemline|hip|ribs?)\b')
    parts=[]
    for item in value.split(','):
        if re.search(hidden,item,re.I):
            # Keep a visible action when only its subordinate detail is outside the frame.
            visible_prefix=''
            for connector in re.finditer(r'\s+(?:while|with|as)\s+',item,re.I):
                prefix=item[:connector.start()]
                if re.search(hidden,prefix,re.I):break
                visible_prefix=prefix
            if not visible_prefix:continue
            item=visible_prefix
        if size=='detail' and re.search(r'\b(?:standing|sitting|seated|kneeling)\b',item,re.I):continue
        parts.append(item.strip())
    return ', '.join(parts)

def outfit(snapshot):
    size=snapshot.get('shot_plan',{}).get('size');parts=[]
    for item in clauses(snapshot.get('outfit_tags','')).split(','):
        # Hair belongs to saved identity, never the reusable clothing vocabulary.
        if re.search(r'\bhair\b|\beyes?\b',item,re.I):continue
        if size=='detail':
            if not re.search(r'\b(?:sleeves?|cuffs?|gloves?|bracelet|watch)\b',item,re.I):continue
        elif size in ('close','extreme_close','medium'):
            if re.search(r'\b(?:slippers?|socks?|shoes?|boots?|stockings?|pantyhose|skirt|pants|trousers)\b',item,re.I):continue
            item=re.sub(r'\blong dress\b','dress bodice',item,flags=re.I)
        if item.strip():parts.append(item.strip())
    return ', '.join(parts)

def camera(plan, simple_background=False):
    if not plan:return ''
    size={'detail':'detail close-up, face outside frame',
          'close':'close-up of face and shoulders','extreme_close':'extreme close-up of face',
          'medium':'waist-up medium shot','full':'full body, head to toe visible, space below both feet',
          'wide':'wide shot, complete figure and surrounding environment','extreme_wide':'extreme wide shot, small figure in a large environment'}[plan['size']]
    direction={'front':'front view','side':'side profile','back':'rear view, face not visible',
        'three_quarter':'three quarter view','over_shoulder':'over the shoulder view from behind'}
    elevation={'eye_level':'eye level','high':'high angle','low':'low angle','overhead':'overhead view','ground_level':'ground level'}
    if simple_background:
        size={'medium':'upper body','close':'portrait, head and shoulders','extreme_close':'face focus'}.get(plan['size'],size)
    parts=[size]
    parts.extend([direction.get(plan.get('direction'),''),elevation.get(plan.get('elevation'),'')])
    parts.append({'centered':'centered composition','thirds':'rule of thirds','off_center':'off center composition',
        'negative_space':'negative space','foreground':'foreground depth','frame':'environmental framing'}.get(plan.get('composition'), '')
        if not simple_background or plan.get('composition')!='foreground' else '')
    if plan.get('stylization') in ('dutch','perspective','comic_deformation'):
        parts.append({'dutch':'dutch angle','perspective':'dynamic foreshortening','comic_deformation':'expressive comic deformation'}[plan['stylization']])
    spatial=re.compile(r'\b(?:tabletop height|table level|through doorway|room framing|subject (?:at|on|off center|small)|foreground (?:activity props|shoulder|occlusion)|props in focus|background detail|negative space on)\b',re.I)
    parts.extend(tag.strip() for tag in plan.get('tags','').split(',') if spatial.search(tag)
                 and (not simple_background or not re.search(r'foreground|props in focus|background detail',tag,re.I)))
    position=plan.get('position')
    if position and not plan.get('user_locked') and position!='center' and not any('subject ' in part for part in parts):
        parts.append('subject positioned at '+position)
    return ', '.join(dict.fromkeys(p for p in parts if p))


def clock_compatible(value,hour):
    # Keep designed light compatible with the established time.
    daylight=r'\b(?:daytime|afternoon sun(?:light)?|daylight|sunlight|sunbeams?|sunrise|sunset|dusk light)\b'
    night=r'\b(?:moonlight|midnight|night(?:time)? sky)\b'
    terms=daylight if hour<5 or hour>=19 else night
    return ', '.join(tag.strip() for tag in value.split(',') if tag.strip() and not re.search(terms,tag,re.I))


def add_detail(base,extra):
    existing = {part.strip().casefold() for part in base.split(',')}
    return ', '.join(part for part in (base, ', '.join(tag.strip() for tag in extra.split(',')
                     if tag.strip() and tag.strip().casefold() not in existing)) if part)


@dataclass
class PromptSpec:
    style: str
    subject: str
    identity: str
    outfit: str
    environment: str
    action: str
    props: str
    expression: str
    atmosphere: str
    camera: str
    scope: dict
    version: str=VERSION
    art_direction: dict | None=None

    def positive(self):
        fields=(art_styles.SINGLE_FRAME,self.style,self.camera,self.subject,self.identity,self.outfit,
                self.environment,self.action,self.props,self.expression,self.atmosphere)
        if self.scope.get('simple_background'):
            style_tags=[tag.strip() for tag in self.style.split(',')]
            quality_tags={'masterpiece','best quality','excellent','very aesthetic','highres','newest'}
            quality=[tag for tag in style_tags if tag in quality_tags]
            artists=[tag for tag in style_tags if '@' in tag]
            general_style=[tag for tag in style_tags if tag not in quality_tags and tag not in artists]
            subject='1girl, solo' if self.subject=='one woman, solo' else self.subject
            fields=(*quality,subject,self.identity,*artists,*general_style,self.outfit,self.camera,
                    self.action,self.props,self.expression,self.environment,self.atmosphere)
            text=', '.join(v.strip(' .') for v in fields if v)
        else:
            text='. '.join(v.strip(' .') for v in fields if v)
        for value in (self.art_direction or {}).values():
            detail=add_detail('',', '.join(part.strip() for part in value.split(',')
                                          if part.strip() and part.strip().casefold() not in text.casefold()))
            if detail and len(text)+len(detail)+2<=MAX_PROMPT_CHARS:
                text+='. '+detail
        return text

    def negative(self,base):
        tags=[t.strip() for t in base.split(',') if t.strip()]
        if self.scope.get('size') in ('detail','close','extreme_close'):tags=[t for t in tags if t!='cropped']
        # Preserve workflow negatives; append only scoped constraints exactly once.
        extra=', '.join(self.scope.get('excluded_objects',[]))
        if self.scope.get('simple_background'):
            extra=add_detail(extra,'busy background, background clutter, distracting foreground objects, (picture-in-picture:1.5), (inset portrait:1.5)')
        for t in (art_styles.LAYOUT_NEGATIVE+', '+shots.negative_tags(self.scope)+', '+extra).split(','):
            if t.strip() and t.strip() not in tags:tags.append(t.strip())
        return ', '.join(tags)

    def record(self):return asdict(self)

    def validate(self):
        required=['style','subject','environment','action']
        if self.scope.get('size')!='detail':required.extend(['identity','outfit'])
        if self.scope.get('size'):required.append('camera')
        missing=[name for name in required if not getattr(self,name).strip()]
        if self.scope.get('has_setting') is False and self.scope.get('size')!='detail' and 'environment' not in missing:
            missing.append('environment')
        if missing:raise ValueError('Missing visible prompt fields: '+', '.join(missing))
        if len(self.positive())>MAX_PROMPT_CHARS:raise ValueError('Positive prompt exceeds the local size limit')


def without_excluded(value,exclusions):
    if not exclusions:return value
    patterns=[re.compile(r'\b'+re.escape(word)+r'(?:s|es)?\b',re.I) for word in exclusions]
    kept=[]
    for part in value.split(','):
        part=part.strip()
        if any(pattern.search(part) for pattern in patterns):
            part=re.split(r'\s+(?:without|not wearing|not holding)\b',part,maxsplit=1,flags=re.I)[0]
            if any(pattern.search(part) for pattern in patterns):continue
        if part:kept.append(part)
    return ', '.join(kept)

def build(snapshot):
    from image_policy import scene_tags
    mode=snapshot['style'];plan=snapshot.get('shot_plan',{})
    style=art_styles.resolve(snapshot.get('art_style','original'))
    prefix=art_styles.STYLES[style]['prefix']
    simple_background=mode=='life' and plan.get('simple_background',False)
    if mode=='life' and style=='original' and not simple_background:
        prefix+=', scnr'
    # Respect established mode templates instead of adding the Life artist mixture to chibi.
    visual_style=(prefix+', '+MODES['life']) if mode=='life' else MODES[mode]+(', '+prefix if style!='original' else '')
    if snapshot.get('purpose')=='poster':visual_style='character poster illustration, poster layout, no lettering, '+visual_style
    hour=snapshot.get('visual_hour',int(snapshot['local_time'][11:13]))
    exclusions=snapshot.get('visual_exclusions',[])
    setting_source=without_excluded(clauses(snapshot.get('setting_tags','')),exclusions)
    period,default_light=scene_tags('',hour,'').split(', ',maxsplit=1)
    clock_tags={period.casefold(),default_light.casefold()}
    creative=snapshot.get('creative_brief',{})
    atmosphere=clock_compatible(without_excluded(clauses(creative.get('atmosphere','')),exclusions),hour)
    atmosphere=', '.join(tag.strip() for tag in atmosphere.split(',')
                         if tag.strip() and tag.strip().casefold() not in clock_tags)
    from image_concept import art_direction
    proposed=art_direction(creative.get('art_direction'))
    designed_light=clock_compatible(without_excluded(clauses(proposed.get('lighting','')),exclusions),hour)
    generic_clock={'daytime','early morning','evening','night','natural daylight','soft morning light',
                   'warm dusk light','ambient artificial lighting'}
    setting=clock_compatible(', '.join(part.strip() for part in setting_source.split(',')
                                     if part.strip().casefold() not in generic_clock),hour)
    has_light=bool(designed_light or re.search(r'\b(?:light|lighting|sunlight|daylight|moonlight|shadow|shadows)\b',
                                             setting+', '+atmosphere,re.I))
    if simple_background and 'setting' not in snapshot.get('visual_overrides',[]) and plan.get('size')!='detail':
        landmarks=[part.strip() for part in setting.split(',') if part.strip()]
        lighting=[part for part in landmarks if re.search(r'\b(?:light|lighting|sunlight|daylight|moonlight|shadows?)\b',part,re.I)]
        anchors=[part for part in landmarks if part not in lighting]
        setting=', '.join(anchors[:2]+lighting)
    setting=add_detail(setting,period if has_light else period+', '+default_light)
    if plan.get('size')=='detail':
        setting=', '.join(t for t in setting.split(',') if re.search(r'\b(?:desk|table|counter|paper|sheet|music|book|soil|sink|stove|cup|bowl|daylight|lighting)\b',t,re.I) and not re.search(r'\b(?:room|floor|video call)\b',t,re.I))
        setting=setting or period
    if simple_background and plan.get('size')!='detail':
        treatment='simple background, subdued background detail'
        if plan.get('size') in ('medium','close','extreme_close'):
            treatment+=', depth of field'
        setting=add_detail(setting,treatment)
    identity=clauses(shots.visible_identity(snapshot)) if plan.get('size')!='detail' else ''
    identity_override='identity' in snapshot.get('visual_overrides',[])
    if not identity_override:
        identity=', '.join(t.strip() for t in identity.split(',') if not re.fullmatch(r'\s*(?:1woman|1girl|solo)\s*',t,re.I))
    identity=without_excluded(identity,exclusions)
    emotion=without_excluded(clauses(shots.visible_emotion(snapshot)),exclusions)
    props=clauses(snapshot.get('props',''))
    props=', '.join(t for t in props.split(',') if not re.search(r'\bno (?:additional )?props\b',t,re.I))
    if plan.get('size') in ('close','extreme_close','detail','medium'):
        props=', '.join(t for t in props.split(',') if not re.search(r'\b(?:slippers?|socks?|shoes?|floor|full body)\b',t,re.I))
    action=without_excluded(visible_action(snapshot),exclusions)
    # Do not restate a prop already related to a hand in the action.
    props=without_excluded(props,exclusions)
    props=', '.join(t.strip() for t in props.split(',') if t.strip() and t.strip().casefold() not in action.casefold())
    clothing=without_excluded(outfit(snapshot),exclusions)
    scope={**plan,'visible_action':action,'visible_props':props,'visible_outfit':clothing,
           'excluded_objects':exclusions,'has_setting':bool(setting_source),'simple_background':simple_background}
    details={}
    for key in ('lighting','pose_geometry','camera_relationship','material_details','foreground_background',
                'facial_micro_expression','visual_focus','color_palette'):
        if simple_background and key=='foreground_background':
            continue
        value=without_excluded(clauses(proposed.get(key,'')),exclusions)
        if key=='lighting':
            value=designed_light
        elif key in ('pose_geometry','material_details','foreground_background'):
            value=visible_action({**snapshot,'action':value})
            if key=='pose_geometry' and (
                re.search(r'\b(?:stand\w*|standing)\b',action,re.I) and re.search(r'\b(?:sitting|seated)\b',value,re.I)
                or re.search(r'\b(?:sitting|seated)\b',action,re.I) and re.search(r'\b(?:stand\w*|standing)\b',value,re.I)
            ):
                value=''
        elif key=='camera_relationship':
            if plan.get('user_locked') or (simple_background and re.search(r'foreground|through a|between objects',value,re.I)) or re.search(
                r'\b(?:full body|wide shot|medium shot|close[ -]?up|high angle|low angle|overhead|rear view|front view|side view|portrait format|landscape format)\b',value,re.I):
                value=''
        elif key=='facial_micro_expression' and not plan.get('expectations',{}).get('face_expected',True):
            value=''
        elif key=='visual_focus':
            value=shots.visible_focus(snapshot,value)
        if value:details[key]=value
    if 'visual_focus' not in details and re.search(r'\b(?:in focus|foreground|highlight|focal|draws? the eye)\b',creative.get('focal_point',''),re.I):
        focus=without_excluded(clauses(shots.visible_focus(snapshot)),exclusions)
        if focus:details['visual_focus']=focus
    subject=plan.get('detail_subject','hands and activity props') if plan.get('size')=='detail' else 'character' if identity_override else 'one woman, solo'
    return PromptSpec(visual_style,subject,identity,clothing,setting,action,props,emotion,atmosphere,camera(plan,simple_background),scope,
                      art_direction=details or None)
