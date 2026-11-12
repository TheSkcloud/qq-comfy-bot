"""Named rendering recipes; independent of character state and narrative modes."""
import copy

SINGLE_FRAME='(single continuous illustration:1.4), (one scene:1.3), one viewpoint, one depiction of the subject, coherent perspective'
LAYOUT_NEGATIVE='(multiple panels:1.5), (split screen:1.5), diptych, triptych, collage, inset panel, panel border, multiple views, repeated character, duplicate body, extra arms, extra hands'

def composition_prompt(text):
    # De-duplicate complete clauses without changing weighted tag syntax.
    parts=list(dict.fromkeys(p.strip() for p in text.split(',') if p.strip()))
    return SINGLE_FRAME+', '+', '.join(parts)

DEFAULT = 'original'
STYLES = {
    'original': {'name':'原版', 'description':'原有 H 画风',
        'prefix':'masterpiece, best quality, very aesthetic, highres, newest, detailed illustration, hand-drawn 2d anime illustration, soft shading, delicate colored outlines, (@ningen mame:1.9), (@meme50:2.1), (@rurudo:1.9), (@piratescat01:1.6)'},
    'cel': {'name':'赛璐璐', 'description':'清晰线条、利落色块与明暗（参考 00072 / 00101）',
        'prefix':'best quality, excellent, 2d anime illustration, @92m',
        'loras':[('Blue_archive_style.safetensors',.6),('bluearchivefull1-000006.safetensors',.9),('BadAnatomy.safetensors',-.75)]},
    'handdrawn': {'name':'手绘', 'description':'带手绘质感的二维上色（参考 00045）',
        'prefix':'best quality, excellent, 2d anime illustration, hand-drawn, @92m',
        'loras':[('Blue_archive_style.safetensors',1.),('Hand-drawnfeel-000018.safetensors',1.),('BadAnatomy.safetensors',-.5)]},
}
ALIASES = {**{k:k for k in STYLES}, '原版':'original','默认':'original','H':'original','h':'original',
           '赛璐璐':'cel','清透赛璐璐':'cel','清透':'cel','手绘':'handdrawn','手绘柔光':'handdrawn'}

def resolve(value):
    try:return ALIASES[value.strip()]
    except (KeyError,AttributeError):raise ValueError('未知画风。可选：原版、赛璐璐、手绘。') from None

def selected(session):
    return resolve(session.get('art_style',DEFAULT))

def describe(value):
    return STYLES[resolve(value)]['name']

def menu(value=DEFAULT):
    return '当前画风：'+describe(value)+'\n'+ '\n'.join('/画风 '+v['name']+'：'+v['description'] for v in STYLES.values())+'\n切换后用于新建图片任务；已排队的图片保留原画风。'

def settings(value):
    value=resolve(value)
    if value==DEFAULT:return {}
    return dict(art_style=value,steps=30,cfg=1.6,width=896,height=1152,reference=False,upscale=False,hand_detailer=True)

def node_id(graph, class_type):
    matches=[key for key,node in graph.items() if node.get('class_type')==class_type]
    if len(matches)!=1:raise ValueError(f'工作流需要唯一的 {class_type} 节点，实际 {len(matches)} 个')
    return matches[0]

def text_node(graph, sampler, name):
    key=graph[sampler]['inputs'][name][0]
    if graph[key]['class_type']!='CLIPTextEncode':raise ValueError('工作流提示词必须连接 CLIPTextEncode')
    return key

def graph_for(graph, value):
    """Build a complete recipe from frozen text/seed; never retain old style LoRAs."""
    value=resolve(value)
    if value==DEFAULT:return copy.deepcopy(graph)
    sampler=node_id(graph,'KSampler');source=graph[sampler]['inputs']
    pos=graph[text_node(graph,sampler,'positive')]['inputs']['text']
    neg=graph[text_node(graph,sampler,'negative')]['inputs']['text']
    def node(cls,**inputs):return {'class_type':cls,'inputs':inputs}
    result={
        '66':node('UNETLoader',unet_name='miaomiaoHarem_29BBETA10.safetensors',weight_dtype='default'),
        '62':node('CLIPLoader',clip_name='oneObsession_anima29BV1_txt.safetensors',type='lumina2',device='default'),
        '63':node('VAELoader',vae_name='qwen_image_vae.safetensors'),
        '74':node('Mahiro',model=['66',0]),
        '67':node('CLIPTextEncode',clip=['62',0],text=pos),
        '71':node('CLIPTextEncode',clip=['62',0],text=neg),
        '68':node('EmptySD3LatentImage',width=896,height=1152,batch_size=1),
        '70':node('KSampler',seed=source['seed'],steps=30,cfg=1.,sampler_name='euler',scheduler='simple',denoise=.9,
                  model=['69',0],positive=['67',0],negative=['71',0],latent_image=['68',0]),
        '65':node('VAEDecode',samples=['70',0],vae=['63',0]),
        '9':node('SaveImage',images=['65',0],filename_prefix='QQBOT/'+value),
    }
    upstream='74'
    for key,(name,strength) in zip(('107','109','150'),STYLES[value]['loras']):
        result[key]=node('LoraLoaderModelOnly',model=[upstream,0],lora_name=name,strength_model=strength)
        upstream=key
    result['69']=node('ModelSamplingAuraFlow',model=[upstream,0],shift=3.)
    return result

def add_detailer(graph,profile):
    if not profile.get('hand_detailer'):return graph
    sampler=node_id(graph,'KSampler');inputs=graph[sampler]['inputs']
    decode=node_id(graph,'VAEDecode');save=node_id(graph,'SaveImage')
    positive=text_node(graph,sampler,'positive');negative=text_node(graph,sampler,'negative')
    # Reuse installed Tulut hand detector + masked crop refinement, no full-image redraw.
    graph['style_upscale']={'class_type':'UpscaleModelLoader','inputs':{'model_name':'4x-UltraSharp.pth'}}
    graph['style_hands']={'class_type':'TulutHandDetailer','inputs':{
        'image':copy.deepcopy(graph[save]['inputs']['images']), 'model':copy.deepcopy(inputs['model']),
        'clip':copy.deepcopy(graph[positive]['inputs']['clip']), 'vae':copy.deepcopy(graph[decode]['inputs']['vae']),
        'upscale_model':['style_upscale',0], 'lllite_name':'none',
        'prompt':'single anatomically coherent hand, five natural fingers, consistent anime linework and shading',
        'neg_prompt':LAYOUT_NEGATIVE+', fused fingers, extra fingers, malformed hand',
        'guide_size':512,'steps':20,'cfg':float(inputs['cfg']),'denoise':.35,'character_index':0,'seed':inputs['seed']}}
    graph[save]['inputs']['images']=['style_hands',0]
    return graph
