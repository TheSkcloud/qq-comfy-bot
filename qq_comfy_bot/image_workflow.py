"""Mode-specific rendering and exact-state reference anchors, without auto-promoting outputs."""
import copy
import hashlib
import json
import re
from pathlib import Path

PROFILES = {
    'life':dict(steps=36,cfg=5.,reference=False,ref_guidance=1.5,ref_end=.65,ref_megapixels=.6,strength=.65),
    'comfort':dict(steps=30,cfg=4.5,reference=False),
    'comic':dict(steps=30,cfg=4.5,reference=False,ref_guidance=1.8,ref_end=.6,ref_megapixels=.6),
}


def signature(snapshot):
    return hashlib.sha256(json.dumps([snapshot['character_id'],snapshot['outfit'],snapshot['hairstyle']],ensure_ascii=False).encode()).hexdigest()


def prepare(root,snapshot):
    profile=copy.deepcopy(PROFILES.get(snapshot['style'],PROFILES['life']))
    file=Path(root)/'render_profiles.json'
    if file.exists():
        profiles=json.loads(file.read_text(encoding='utf-8'))
        profile.update(profiles.get(snapshot['style'],{}))
    bank=Path(root)/'data'/'reference_anchors.json'
    anchor=json.loads(bank.read_text(encoding='utf-8')).get(signature(snapshot)) if bank.exists() else None
    if profile.get('reference') and anchor and Path(anchor['path']).is_file():
        profile['reference_path']=anchor['path']
        profile['reference_sha256']=hashlib.sha256(Path(anchor['path']).read_bytes()).hexdigest()
        profile['reference_scope']='same_character_outfit_hairstyle'
    else:
        profile['reference']=False
    if snapshot.get('purpose')=='avatar':
        profile['square_avatar']=True
    shot=snapshot.get('shot_plan',{})
    if shot:
        width=int(profile.get('width',768));height=int(profile.get('height',1152))
        orientation=shot.get('orientation')
        if orientation=='landscape':profile.update(width=max(width,height),height=min(width,height))
        elif orientation=='square':profile.update(width=min(width,height),height=min(width,height))
        elif orientation=='portrait':profile.update(width=min(width,height),height=max(width,height))
        profile['shot_plan']=copy.deepcopy(shot)
    return profile


def prompt(snapshot,profile,legacy):
    if snapshot['style']=='life':
        from shot_director import guide_prompt
        if snapshot.get('purpose')=='poster':legacy='character poster illustration, poster layout, character as the main subject, no lettering, '+legacy
        return guide_prompt(snapshot,legacy)
    if snapshot['style']=='comfort':
        prefix='colored pencil drawing on cream textured paper, small hand-drawn comforting gift, chibi, simplified expressive linework, soft pastel colors, single panel, no lettering'
    elif snapshot['style']=='comic':
        prefix='playful single-panel manga, chibi proportions, expressive eyebrows, clearly drawn mouth, silly visual gag, expressive motion lines, clean ink drawing, no lettering'
        if not re.search(r'open mouth|laugh|shout|scream',snapshot.get('emotion',''),re.I):
            prefix+=', closed mouth, deadpan comic expression'
    else:
        prefix='masterpiece, best quality, detailed anime illustration, 1woman, solo, candid everyday moment'
    # Explicit identity/outfit tags remain mandatory on this local fine-tune:
    # the scene-only adapter recipe changed hair/eyes/outfit in actual trials.
    parts=[prefix]
    from shot_director import visible_identity,visible_outfit,visible_action,visible_emotion,visible_focus
    parts.extend([visible_identity(snapshot),visible_outfit(snapshot)])
    parts.extend([snapshot['setting_tags'],visible_action(snapshot),snapshot['props'],visible_emotion(snapshot),snapshot['camera']])
    creative=snapshot.get('creative_brief',{})
    parts.extend([creative.get('atmosphere',''),visible_focus(snapshot)])
    hour=int(snapshot['local_time'][11:13])
    parts.append('night, indoor warm lighting' if hour>=19 or hour<6 else 'daytime, natural lighting')
    from shot_director import guide_prompt
    return guide_prompt(snapshot,', '.join(p for p in parts if p))


async def apply(http,url,graph,profile):
    graph=copy.deepcopy(graph)
    width=int(profile.get('width',768));height=int(profile.get('height',1152))
    if not (512<=width<=1536 and 512<=height<=2048 and width%64==0 and height%64==0):
        raise ValueError('Render dimensions must be bounded multiples of 64')
    if profile.get('square_avatar'):height=width
    graph['68']['inputs'].update(width=width,height=height)
    if not profile.get('upscale',False):
        # Save decoded native image; remove orphan upscaler nodes to avoid loading it.
        graph['9']['inputs']['images']=['65',0]
        for node in ('180','181','182'):graph.pop(node,None)
    graph['70']['inputs'].update(steps=int(profile.get('steps',36)),cfg=float(profile.get('cfg',5)))
    if profile.get('shot_plan') and '71' in graph:
        from shot_director import negative_tags
        plan=profile['shot_plan']
        negative=graph['71']['inputs']['text']
        if plan.get('size') in ('detail','close','extreme_close'):
            negative=', '.join(t.strip() for t in negative.split(',') if t.strip()!='cropped')
        graph['71']['inputs']['text']=', '.join(v for v in (negative,negative_tags(plan)) if v)
    if profile.get('reference'):
        import aiohttp
        path=Path(profile['reference_path'])
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=profile['reference_sha256']:
            raise ValueError('Frozen reference image has changed')
        form=aiohttp.FormData()
        name='qq_anchor_'+profile['reference_sha256'][:20]+'.png'
        form.add_field('image',content,filename=name,content_type='image/png')
        form.add_field('type','input');form.add_field('overwrite','false')
        async with http.post(url+'/upload/image',data=form) as response:
            response.raise_for_status();uploaded=await response.json()
        image='/'.join(p for p in (uploaded.get('subfolder',''),uploaded['name']) if p)
        graph['201']={'class_type':'LoadImage','inputs':{'image':image}}
        # Sampling shift and all existing model/style LoRAs remain upstream.
        model=graph['70']['inputs']['model']
        graph['202']={'class_type':'AnimaRefAdapter','inputs':{
            'model':model,'vae':['63',0],'image':['201',0],
            'lora_name':'anima_ref_adapter_v2_r64.safetensors','ref_guidance':float(profile['ref_guidance']),
            'correction':'apg','strength':float(profile.get('strength',1.)),'ref_end':float(profile['ref_end']),
            'ref_megapixels':float(profile['ref_megapixels'])}}
        graph['70']['inputs']['model']=['202',0]
    return graph
