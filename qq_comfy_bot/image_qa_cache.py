"""Immutable-image/semantic-target QA receipts; never retry a known verdict for luck."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import weakref
from datetime import datetime
import io

RUBRIC='visual-qa-v4-prompt-scope'
_loops=weakref.WeakKeyDictionary()

def key(content,snapshot):
    fields=('character_id','identity','location','outfit','hairstyle','outfit_tags','setting_tags','action','props','emotion','style','expected_art_style','purpose')
    target={field:snapshot.get(field) for field in fields}
    target['render_target']=snapshot.get('render_target')
    target['state']=snapshot.get('state',{})
    shot=snapshot.get('shot_plan') or {}
    target['shot']={field:shot.get(field) for field in ('size','direction','elevation','composition','expectations','user_locked')}
    creative=snapshot.get('creative_brief') or {}
    target['creative']={field:creative.get(field) for field in ('subject','action','expression','gesture','props','environment','atmosphere','focal_point')}
    try:
        hour=datetime.fromisoformat(snapshot.get('local_time','')).hour
        target['lighting_period']='morning' if 5<=hour<8 else 'day' if 8<=hour<17 else 'evening' if 17<=hour<19 else 'night'
    except (ValueError,TypeError):target['lighting_period']=None
    target['rubric']=RUBRIC;target['model']=os.getenv('DEEPSEEK_MODEL','deepseek-flash');target['provider']=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com').rstrip('/')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as original:
            pixels=original.convert('RGB')
            digest=hashlib.sha256((str(pixels.size)+'|RGB|').encode()+pixels.tobytes()).hexdigest()
    except Exception:digest=hashlib.sha256(content).hexdigest()
    target_digest=hashlib.sha256(json.dumps(target,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    identifier=hashlib.sha256((digest+target_digest).encode()).hexdigest()
    return identifier,digest,target_digest

def lock(root,identifier):
    loop=asyncio.get_running_loop();items=_loops.setdefault(loop,{})
    return items.setdefault((str(Path(root).resolve()).casefold(),identifier),asyncio.Lock())

def read(root,identifier,digest,target_digest):
    try:
        record=json.loads((Path(root)/'image_qa_cache'/f'{identifier}.json').read_text(encoding='utf-8'))
        result=record['result']
        if record.get('image_pixels_sha256')!=digest or record.get('target_sha256')!=target_digest or record.get('rubric')!=RUBRIC:return None
        if not isinstance(result,dict) or result.get('status')!='checked' or type(result.get('pass')) is not bool:return None
        if result['pass'] and (not all(result.get(k) is True for k in ('identity_ok','scene_ok','anatomy_ok','style_ok')) or result.get('uncertain') is not False or not isinstance(result.get('findings'),list) or any(not isinstance(item,dict) or item.get('severity')=='major' for item in result['findings'])):return None
        return {**copy.deepcopy(result),'cache_hit':True,'qa_receipt_id':identifier,'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}}
    except (OSError,ValueError,KeyError,TypeError):return None

def write(root,identifier,digest,target_digest,result):
    if result.get('status')!='checked':return
    path=Path(root)/'image_qa_cache';path.mkdir(parents=True,exist_ok=True)
    destination=path/f'{identifier}.json'
    if not destination.exists() and sum(1 for _ in path.glob('*.json'))>=512:return
    record={'rubric':RUBRIC,'image_pixels_sha256':digest,'target_sha256':target_digest,'result':result}
    temporary=destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(destination)
