"""One scene-to-prompt compiler. No model calls or changes to render settings."""
from dataclasses import dataclass, asdict
import re
import art_styles
import shot_director as shots

VERSION='prompt-spec-v2'
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
        item=re.sub(r'\b(?:long|short|blue[ -](?:violet|purple))\s+hair\b[^,]*','',item,flags=re.I)
        if re.search(r'\bhair\b|\beyes?\b',item,re.I):continue
        if size=='detail':
            if not re.search(r'\b(?:sleeves?|cuffs?|gloves?|bracelet|watch)\b',item,re.I):continue
        elif size in ('close','extreme_close','medium'):
            if re.search(r'\b(?:slippers?|socks?|shoes?|boots?|stockings?|pantyhose|skirt|pants|trousers)\b',item,re.I):continue
            item=re.sub(r'\blong dress\b','dress bodice',item,flags=re.I)
        if item.strip():parts.append(item.strip())
    return ', '.join(parts)

def camera(plan):
    if not plan:return ''
    size={'detail':'detail close-up, face outside frame',
          'close':'close-up of face and shoulders','extreme_close':'extreme close-up of face',
          'medium':'waist-up medium shot','full':'full body, head to toe visible, space below both feet',
          'wide':'wide shot, complete figure and surrounding environment','extreme_wide':'extreme wide shot, small figure in a large environment'}[plan['size']]
    direction={'front':'front view','side':'side profile','back':'rear view, face not visible',
        'three_quarter':'three quarter view','over_shoulder':'over the shoulder view from behind'}
    elevation={'eye_level':'eye level','high':'high angle','low':'low angle','overhead':'overhead view','ground_level':'ground level'}
    parts=[size]
    parts.extend([direction.get(plan.get('direction'),''),elevation.get(plan.get('elevation'),'')])
    parts.append({'centered':'centered composition','thirds':'rule of thirds','off_center':'off center composition',
        'negative_space':'negative space','foreground':'foreground depth','frame':'environmental framing'}.get(plan.get('composition'),''))
    if plan.get('stylization') in ('dutch','perspective','comic_deformation'):
        parts.append({'dutch':'dutch angle','perspective':'dynamic foreshortening','comic_deformation':'expressive comic deformation'}[plan['stylization']])
    return ', '.join(p for p in parts if p)

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

    def positive(self):
        return '. '.join(v.strip(' .') for v in (art_styles.SINGLE_FRAME,self.style,self.camera,
            self.subject,self.identity,self.outfit,self.environment,self.action,self.props,self.expression,self.atmosphere) if v)

    def negative(self,base):
        tags=[t.strip() for t in base.split(',') if t.strip()]
        if self.scope.get('size') in ('detail','close','extreme_close'):tags=[t for t in tags if t!='cropped']
        # Preserve workflow negatives; append only scoped constraints exactly once.
        extra=', '.join(self.scope.get('excluded_objects',[]))
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
    if mode=='life':
        prefix=prefix+', scnr' if style=='original' else prefix.replace(', @92m','')
    # Respect established mode templates instead of adding the Life artist mixture to chibi.
    visual_style=(prefix+', '+MODES['life']) if mode=='life' else MODES[mode]+(', '+prefix if style!='original' else '')
    if snapshot.get('purpose')=='poster':visual_style='character poster illustration, poster layout, no lettering, '+visual_style
    hour=snapshot.get('visual_hour',int(snapshot['local_time'][11:13]))
    exclusions=snapshot.get('visual_exclusions',[])
    setting_source=without_excluded(clauses(snapshot.get('setting_tags','')),exclusions)
    setting=scene_tags(setting_source,hour,'')
    # One clock-derived lighting clause; retain place nouns separately.
    setting_parts=setting.split(',')
    setting=', '.join(t.strip() for t in setting_parts[:-2] if not re.search(r'\b(?:light|lighting|daylight|sunlight|midday|night|daytime|dusk|morning)\b',t,re.I))
    from image_policy import scene_tags as with_clock
    setting=with_clock(setting,hour,'')
    if plan.get('size')=='detail':
        setting=', '.join(t for t in setting.split(',') if re.search(r'\b(?:desk|table|counter|paper|sheet|music|book|soil|sink|stove|cup|bowl|daylight|lighting)\b',t,re.I) and not re.search(r'\b(?:room|floor|video call)\b',t,re.I))
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
           'excluded_objects':exclusions,'has_setting':bool(setting_source)}
    atmosphere=clauses(snapshot.get('creative_brief',{}).get('atmosphere',''))
    atmosphere=', '.join(t.strip() for t in atmosphere.split(',') if not re.search(r'\b(?:light|lighting|daylight|sunlight|midday|night|daytime|dusk|morning)\b',t,re.I))
    atmosphere=without_excluded(atmosphere,exclusions)
    subject=plan.get('detail_subject','hands and activity props') if plan.get('size')=='detail' else 'character' if identity_override else 'one woman, solo'
    return PromptSpec(visual_style,subject,identity,clothing,setting,action,props,emotion,atmosphere,camera(plan),scope)
