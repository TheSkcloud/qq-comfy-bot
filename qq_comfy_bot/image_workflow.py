"""Mode-specific rendering and exact-state reference anchors, without auto-promoting outputs."""
import copy
import hashlib
import json
import re
from pathlib import Path
import art_styles

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
    style=art_styles.resolve(snapshot.get('art_style','original'))
    profile.update(art_styles.settings(style))
    if style!='original' and snapshot['style']!='life':
        # Keep the deliberate chibi/manga canvas and mode semantics.
        mode=profiles.get(snapshot['style'],{}) if file.exists() else {}
        profile.update(width=mode.get('width',576),height=mode.get('height',832))
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


def prompt(snapshot,profile,legacy=''):
    from prompt_spec import build
    spec=build(snapshot)
    profile['prompt_spec']=spec.record()
    return spec.positive()


async def apply(http,url,graph,profile):
    graph=art_styles.graph_for(graph,profile.get('art_style','original'))
    sampler=art_styles.node_id(graph,'KSampler')
    latent=graph[sampler]['inputs']['latent_image'][0]
    negative_node=art_styles.text_node(graph,sampler,'negative')
    decode=art_styles.node_id(graph,'VAEDecode')
    save=art_styles.node_id(graph,'SaveImage')
    width=int(profile.get('width',768));height=int(profile.get('height',1152))
    if not (512<=width<=1536 and 512<=height<=2048 and width%16==0 and height%16==0):
        raise ValueError('Render dimensions must be bounded multiples of 16')
    if profile.get('square_avatar'):height=width
    graph[latent]['inputs'].update(width=width,height=height,batch_size=1)
    if not profile.get('upscale',False):
        # Save decoded native image; remove orphan upscaler nodes to avoid loading it.
        graph[save]['inputs']['images']=[decode,0]
        # Drop only nodes no longer reachable from outputs; custom node IDs are supported.
        reachable=set()
        def visit(key):
            if key in reachable:return
            reachable.add(key)
            for v in graph[key]['inputs'].values():
                if isinstance(v,list) and len(v)==2 and isinstance(v[0],str) and isinstance(v[1],int):visit(v[0])
        visit(save)
        graph={k:v for k,v in graph.items() if k in reachable}
    graph[sampler]['inputs'].update(steps=int(profile.get('steps',36)),cfg=float(profile.get('cfg',5)))
    if profile.get('prompt_spec'):
        from prompt_spec import PromptSpec
        spec=PromptSpec(**profile['prompt_spec'])
        graph[negative_node]['inputs']['text']=spec.negative(graph[negative_node]['inputs']['text'])
    else:
        # Compatibility for callers with a graph-only render config.
        from shot_director import negative_tags
        plan=profile.get('shot_plan',{})
        negative=graph[negative_node]['inputs']['text']
        if plan.get('size') in ('detail','close','extreme_close'):
            negative=', '.join(t.strip() for t in negative.split(',') if t.strip()!='cropped')
        graph[negative_node]['inputs']['text']=negative+', '+art_styles.LAYOUT_NEGATIVE+', '+negative_tags(plan)
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
        model=graph[sampler]['inputs']['model']
        graph['202']={'class_type':'AnimaRefAdapter','inputs':{
            'model':model,'vae':graph[decode]['inputs']['vae'],'image':['201',0],
            'lora_name':'anima_ref_adapter_v2_r64.safetensors','ref_guidance':float(profile['ref_guidance']),
            'correction':'apg','strength':float(profile.get('strength',1.)),'ref_end':float(profile['ref_end']),
            'ref_megapixels':float(profile['ref_megapixels'])}}
        graph[sampler]['inputs']['model']=['202',0]
    return art_styles.add_detailer(graph,profile)
