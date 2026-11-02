"""NapCat forward WebSocket + DeepSeek + the supplied ComfyUI workflow."""
import argparse
import hashlib
import contextvars
import asyncio
import base64
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
import services
import dialogue
import preferences
import speech
import response_plan
import media_input
import image_director
import image_workflow
import shot_director
import dialogue_depth
import image_progress
import image_concept
from gpu_memory_manager import GPUManager
from image_errors import ImageFailure
from reply_scheduler import ReplyScheduler, CURRENT, Superseded
from companion import character_key, conversation_direction
from visual_brief import validate_brief, cache_context, assemble, complete_from_cache
from workflow_validation import validate_graph
from image_policy import image_requested, visual_request_context, picture_count, allow_spontaneous, scene_tags
from context_engine import ContextEngine, is_owner
from plugins.onenote import read_notes, format_notes

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
log = logging.getLogger('qqbot')
reply_timing = contextvars.ContextVar('reply_timing', default=None)

def response_delay(read_length, reply_length, elapsed):
    return max(0.0, min(12.0, max(0.8, read_length * 0.055 + reply_length * 0.11)) - elapsed)
FIELDS = ('character', 'clothing', 'location')
LEGACY_WARM_CHARACTER='铃木羽那（Suzuki Hana），偶像大师 闪耀色彩，18岁，蓝紫色长发、金色眼睛，温柔开朗'
DEFAULT = dict(character='铃木羽那（Suzuki Hana），偶像大师 闪耀色彩，18岁，蓝紫色长发、金色眼睛，初识冷淡克制、有边界感，熟悉后逐渐放松', clothing='舒适自然的日常服装', location='根据现实时间自然安排的虚拟日常场景')
OLD_DEFAULT = dict(character='星井美希，成年角色，金发绿眼、长发、呆毛，性格开朗亲切', clothing='蓝色衬衫、蓝色裙子、黄色帽子、白色连裤袜、挎包', location='安静的城市街道')

def daily_appearance(day, character):
    if not any(name in character.lower() for name in ('铃木羽那','鈴木羽那','suzuki hana')):
        return ''
    options = ['', '', '', '', 'hair in a loose ponytail', 'hair in a loose braid', 'hair half up', 'a subtle hair clip']
    value=int.from_bytes(hashlib.sha256(('hana:'+day).encode()).digest()[:4],'big')
    return options[value % len(options)]

def mood_picture(tone):
    if '轻快' in tone: return ('cheerful expression, bright smile', 3)
    if '低缓' in tone: return ('thoughtful expression, quiet mood', 1)
    if '直接' in tone or '简短' in tone: return ('serious expression, calm gaze', 1)
    return ('natural gentle expression', 2 if secrets.randbelow(2) else 1)

HELP = '''可以直接和我聊天，例如：换一件白色连衣裙，去海边拍张照。
/角色 角色名字和外貌性格（切换时保留当前服装和地点）
/换装 服装描述
/地点 场景描述
/拍照 [动作、构图或表情]
/状态 查看设定与现实时间
/时间 查看程序实时读取的北京时间
/笔记 [关键词] 只读查询本机 OneNote
/档案 查看你明确说过的基本信息
/忘记 完整条目内容
/更正 原条目 => 新条目
/取消 停止当前待发送回复与图片
/日程 查看角色今天的虚拟日程
/纪念日 [名称 MM-DD] 登记或查看纪念日
/忘记纪念日 完整名称
/相处 慢慢来|朋友|恋人
/随手图 开|关
/重置 恢复默认角色并清空聊天记忆
/帮助 查看说明
时间跟随现实；更改设定后可用 /拍照 查看。'''


def ids(name):
    return {int(x.strip()) for x in os.getenv(name, '').split(',') if x.strip()}


def now():
    return datetime.now(ZoneInfo(os.getenv('TIMEZONE', 'Asia/Shanghai')))


def lighting(hour):
    if 5 <= hour < 8:
        return 'early morning, dawn, soft morning light'
    if 8 <= hour < 17:
        return 'daytime, natural daylight'
    if 17 <= hour < 19:
        return 'evening, dusk, warm sunset light'
    return 'night, dark sky, ambient artificial lighting'


def visual_prompt(tags, hour, appearance='', mood='natural gentle expression', character='', frozen=False):
    # H 的固定风格前缀；角色、活动、服装和背景由本轮上下文生成。
    prefix = ('masterpiece, best quality, very aesthetic, highres, newest, detailed illustration, '
              'hand-drawn 2d anime illustration, soft shading, delicate colored outlines, '
              '(@ningen mame:1.9), (@meme50:2.1), (@rurudo:1.9), (@piratescat01:1.6)')
    clean=[]
    for tag in scene_tags(tags, hour, '' if frozen else character).split(','):
        tag=tag.strip()
        if not tag or any(x in tag.lower() for x in ('photorealistic','3d render','cgi','@92m','@bluearchstyle','hdr','shiny skin')):
            continue
        if tag.lower() not in {x.lower() for x in clean}: clean.append(tag)
    return ', '.join([prefix]+clean+([appearance] if appearance else [])+[mood,'scnr'])



class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, value TEXT)')

    def get(self, key):
        row = self.db.execute('SELECT value FROM sessions WHERE key=?', (key,)).fetchone()
        value=json.loads(row[0]) if row else dict(state=DEFAULT.copy(), history=[])
        if value['state'].get('character')==LEGACY_WARM_CHARACTER:
            value['state']['character']=DEFAULT['character']
        return value

    def save(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)',
                        (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()


def convert(workflow, info):
    """Use actual backend input schemas, never positional UI widget guessing."""
    links = {x[0]: x for x in workflow['links']}
    result = {}
    for node in workflow['nodes']:
        nid, cls = str(node['id']), node['type']
        # Prompt assembly is performed in Python; bypass both UI helper nodes.
        if node['id'] in (139, 143):
            continue
        if cls not in info:
            raise ValueError(f'ComfyUI 缺少节点：{cls}（ID {nid}），请安装原工作流依赖')
        schema = info[cls]['input']
        required = schema.get('required', {})
        allowed = {**required, **schema.get('optional', {})}
        values = node.get('widgets_values_named', {})
        inputs = {k: copy.deepcopy(v) for k, v in values.items() if k in allowed}
        for socket in node.get('inputs', []):
            if socket.get('link') is not None:
                link = links[socket['link']]
                inputs[socket['name']] = [str(link[1]), link[2]]
        if nid == '67':
            inputs['text'] = ''
        if nid == '68':
            inputs['batch_size'] = 1
        if nid == '9':
            inputs['filename_prefix'] = 'QQAssistant'
        for name, spec in allowed.items():
            if name not in inputs:
                options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                if 'default' in options:
                    inputs[name] = options['default']
                elif name in required:
                    raise ValueError(f'节点 {nid}/{cls} 缺少参数 {name}，请检查插件版本或重新导出工作流')
            if name in inputs and isinstance(spec[0], list) and inputs[name] not in spec[0]:
                raise ValueError(f'节点 {nid}/{cls} 的 {name}={inputs[name]!r} 不在服务端选项中，请检查模型文件名')
        result[nid] = {'class_type': cls, 'inputs': inputs}
    return result


class Comfy:
    def __init__(self, http):
        self.http = http
        self.url = os.getenv('COMFYUI_URL', 'http://127.0.0.1:8188').rstrip('/')
        self.lock = asyncio.Lock()
        self.template = None
        self.gpu=GPUManager(http,self.url,ROOT/'data')

    async def check(self):
        async with self.http.get(self.url + '/object_info') as r:
            r.raise_for_status()
            info = await r.json()
        h_path = ROOT / 'workflow_h_api.json'
        if h_path.exists():
            self.template = json.loads(h_path.read_text(encoding='utf-8-sig'))
            for nid, node in self.template.items():
                if node['class_type'] not in info:
                    raise ValueError(f'H工作流缺少节点 {nid}/{node["class_type"]}')
        else:
            workflow = json.loads((ROOT / 'workflow.json').read_text(encoding='utf-8-sig'))
            self.template = convert(workflow, info)
        validate_graph(self.template, info)
        log.info('工作流检查通过：%d 个 API 节点', len(self.template))

    async def image(self, prompt, job=None):
        try:
            async with self.gpu.execution():
                return await self._image(prompt,job)
        except ImageFailure as exc:
            pid=job.data.get('prompt_id') if job else None
            if pid and exc.code=='generation_timeout':
                try:
                    async with self.http.post(self.url+'/queue',json={'delete':[pid]},timeout=aiohttp.ClientTimeout(total=3)) as response:
                        await response.read()
                    job.record('gpu_cancel_requested',prompt_id=pid,scope='queued_only',reason='generation_timeout')
                except (aiohttp.ClientError,asyncio.TimeoutError):pass
            raise
        except asyncio.CancelledError:
            pid=job.data.get('prompt_id') if job else None
            if pid:
                try:
                    async with self.http.post(self.url+'/queue',json={'delete':[pid]},timeout=aiohttp.ClientTimeout(total=3)) as r:
                        await r.read()
                    job.record('gpu_cancel_requested',prompt_id=pid,scope='queued_only')
                except Exception:
                    pass
            raise

    async def _image(self, prompt, job=None):
        async with self.lock:
            stage='service'
            pid=job.data.get('prompt_id') if job else None
            task_id=job.id if job else uuid.uuid4().hex
            try:
                if not pid:
                    await services.ensure_comfy(self.http, self.url)
                    stage='workflow'
                    if self.template is None:
                        await self.check()
                    graph = copy.deepcopy(self.template)
                    graph['67']['inputs']['text'] = prompt
                    graph['70']['inputs']['seed'] = secrets.randbelow(2**53)
                    if job and job.data.get('render_config'):
                        graph=await image_workflow.apply(self.http,self.url,graph,job.data['render_config'])
                    if job and 'test_seed' in job.data:
                        graph['70']['inputs']['seed']=job.data['test_seed']
                    stage='submit'
                    self.gpu.submitting(task_id)
                    async with self.http.post(self.url + '/prompt', json={
                            'prompt': graph, 'client_id':task_id},
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
                        data = await r.json()
                        if 400<=r.status<500 or data.get('node_errors'):
                            self.gpu.not_submitted(task_id)
                            raise ImageFailure('workflow_rejected','submit',json.dumps(data,ensure_ascii=False)[:1800])
                        if r.status>=500 or not data.get('prompt_id'):
                            raise ImageFailure('submission_unknown','submit','ComfyUI submission receipt is incomplete; do not resubmit blindly')
                    pid = data['prompt_id']
                    self.gpu.submitted(task_id,pid)
                    if job:
                        job.record('submitted',prompt_id=pid,graph=graph)
                    out = ROOT / 'data' / 'images'
                    out.mkdir(parents=True, exist_ok=True)
                    receipt_path = out / f'{pid}.json'
                    receipt = {'image_task_id':job.id if job else None,'prompt_id':pid,'created_at':now().isoformat(timespec='seconds'),
                               'seed':graph['70']['inputs']['seed'],'status':'submitted','graph':graph}
                    receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                else:
                    out=ROOT/'data'/'images';receipt_path=out/f'{pid}.json'
                    receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
                    if job:job.record('resuming',reason='resume_existing_prompt',prompt_id=pid)
                deadline = time.monotonic() + float(os.getenv('IMAGE_TIMEOUT', '600'))
                while time.monotonic() < deadline:
                    stage='history'
                    await asyncio.sleep(1)
                    async with self.http.get(self.url + '/history/' + pid,timeout=aiohttp.ClientTimeout(total=15)) as r:
                        r.raise_for_status()
                        entry = (await r.json()).get(pid)
                    if not entry:continue
                    status = entry.get('status', {})
                    if status.get('status_str') == 'error':
                        self.gpu.completed(task_id)
                        receipt['status']='gpu_error'
                        receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                        raise ImageFailure('gpu_execution_failed','gpu','ComfyUI 执行失败：'+json.dumps(status.get('messages'),ensure_ascii=False)[:1600])
                    images = entry.get('outputs', {}).get('9', {}).get('images', [])
                    if images:
                        stamps=[m[1].get('timestamp',0)/1000 for m in status.get('messages',[]) if isinstance(m,list) and len(m)>1 and m[0]=='execution_success' and isinstance(m[1],dict)]
                        end=max(stamps,default=0)
                        self.gpu.completed(task_id,end if 0<end<=time.time() else None)
                        stage='download'
                        if job:job.record('downloading',reason='gpu_output_ready',output=images[0])
                        async with self.http.get(self.url + '/view', params=images[0],timeout=aiohttp.ClientTimeout(total=30)) as r:
                            r.raise_for_status();content = await r.read()
                        (out / f'{pid}.png').write_bytes(content)
                        if job:job.record('generated',image=f'{pid}.png')
                        receipt.update(status='completed',finished_at=now().isoformat(timespec='seconds'),image=f'{pid}.png')
                        receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                        return content
                    if status.get('completed'):
                        self.gpu.completed(task_id)
                        raise ImageFailure('gpu_output_missing','gpu','Save node 9 completed without an image')
                raise ImageFailure('generation_timeout','history',f'Prompt {pid} exceeded image deadline; GPU may still finish')
            except asyncio.CancelledError:
                raise
            except ImageFailure:
                raise
            except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
                # Submission may have reached ComfyUI without a response. Never resubmit blindly.
                safe=stage!='submit' or isinstance(exc,aiohttp.ClientConnectorError)
                if stage=='submit' and safe:self.gpu.not_submitted(task_id)
                raise ImageFailure({'service':'service_unavailable','workflow':'workflow_check_unavailable','submit':'submission_unknown',
                                    'history':'history_unavailable','download':'download_failed'}[stage],stage,str(exc),safe) from exc
            except (ValueError,KeyError,TypeError) as exc:
                raise ImageFailure('workflow_invalid' if stage=='workflow' else 'invalid_service_response',stage,str(exc)) from exc
            except RuntimeError as exc:
                raise ImageFailure('service_not_ready' if stage=='service' else 'workflow_invalid' if stage=='workflow' else 'generation_failed',stage,str(exc)) from exc


async def deepseek(http, state, history, user, force_image=False, context='', media=None,depth=None):
    preparation_started=time.monotonic()
    current = now()
    from chat_contract import SYSTEM,persona_text
    system_base=SYSTEM
    persona_path=Path(__file__).with_name('persona.md')
    if persona_path.exists():system_base+='\n'+persona_text(persona_path)
    system_base+='\n'+conversation_direction(user,history)
    system=system_base
    system += '\n' + dialogue.clock_context(current, history)
    system += '\n当前设定（JSON数据）：' + json.dumps(state, ensure_ascii=False)
    if force_image:
        system += '\n本次明确要求生成图片，务必提供完整英文 visual_tags。'
    body = dict(model=os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
                messages=[{'role': 'system', 'content': system}] + dialogue.api_history(history,assistant_json=True) +
                         [{'role': 'user', 'content': user}],
                response_format={'type': 'json_object'}, max_tokens=1800)
    if media and media.get('blocks'):
        body['messages'][-1]['content']=[{'type':'text','text':user}]+media['blocks']
    url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions'
    depth=depth or dialogue_depth.plan({},user,history,current,preferences.load().get('thinking_after_substantive_turns',4))
    thinking=force_image or dialogue_depth.should_think(depth)
    body['thinking']={'type':'enabled' if thinking else 'disabled'}
    if thinking:body.update(reasoning_effort='medium',max_tokens=6000 if force_image else 8192 if depth['stage']=='C' else 4096)
    system_base+='\n'+dialogue_depth.context(depth)
    if force_image:system_base+='\n'+image_concept.instruction()
    preparation_ms=round((time.monotonic()-preparation_started)*1000)
    attempts=2 if os.getenv('CHAT_SCHEMA_RETRY','0')=='1' else 1
    for attempt in range(attempts):
        # Retry may take seconds: refresh clock facts for every actual API request.
        body['messages'][0]['content'] = system_base + '\n' + dialogue.clock_context(now(), history)
        body['messages'][0]['content'] += '\n当前设定（JSON数据）：' + json.dumps(state, ensure_ascii=False)
        if context:
            body['messages'][0]['content'] += '\n<本轮背景，仅供相关时使用>\n' + context + '\n</本轮背景>'
        if force_image:
            body['messages'][0]['content'] += '\n本次明确要求生成图片，务必提供完整英文 visual_tags 和 waiting_lines。'
            body['messages'][0]['content'] += '\n这是用户明确的视觉请求，也可能承接上一轮的动作/海报等话题。不要求用户再说一次拍照或画图，不回复“想看就直说”、不把提供插画推迟到某天整理；以当前角色、服装和地点呈现想看的画面，不声称持有真实照片或官方海报。'
            body['messages'][0]['content'] += '\n历史assistant消息为了节省上下文只保留reply，不是本轮输出模板。本轮必须另给send_image:true、完整visual_tags和完整visual_scene五字段，不能只给reply。'
        body['messages'][0]['content'] += '\n<本轮聊天表达，适用于reply字段>\n'+speech.instruction(user, history)+'\n</本轮聊天表达>'
        api_started=time.monotonic()
        async with http.post(url, json=body, headers={
                'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY']}) as r:
            if r.status >= 400:
                raise RuntimeError(f'DeepSeek HTTP {r.status}，请检查密钥、余额和模型名称')
            response = await r.json()
        try:
            choice = response['choices'][0]
            if not isinstance(choice,dict) or not isinstance(choice.get('message'),dict):raise ValueError('Invalid model message envelope')
            if not isinstance(choice['message'].get('content'),str):raise ValueError('Missing model content')
            if choice.get('finish_reason') == 'length':
                raise ValueError('JSON 被截断')
            data = response_plan.decode(choice['message']['content'],force_image)
            parsing_started=time.monotonic()
            data=response_plan.normalize(data,state,user,force_image)
            if data.get('creative_brief'):
                data['creative_brief_raw']=copy.deepcopy(data['creative_brief'])
                data['creative_brief']=image_concept.normalize(data['creative_brief'],data.get('visual_scene'),data.get('image_plan'))
            data['_thinking_enabled']=thinking
            data['_thinking_stage']=depth['stage']
            data['_thinking_evidence']={'requested':thinking,'provider_reasoning_present':bool(choice['message'].get('reasoning_content')),
                'reasoning_tokens':response.get('usage',{}).get('completion_tokens_details',{}).get('reasoning_tokens')}
            import social_reasoning
            data=social_reasoning.normalize(data,user,history)
            data['_stage_times']={'prompt_build_ms':preparation_ms,'api_ms':round((parsing_started-api_started)*1000),'reply_planning_ms':round((time.monotonic()-parsing_started)*1000)}
            if data.get('_model_format')=='plain_text_recovered':
                log.info('模型纯正文已本地恢复，未重试 API；保持原状态且不触发生图')
            grounding_flags=social_reasoning.grounding_issues(data['reply'],user,history)
            from message_style import empty_important_reaction
            if empty_important_reaction(data,user,history):grounding_flags.append('重要事件却只有机械确认，请给一句贴着事情的简短态度，不要扩写安慰或说教')
            if grounding_flags:
                data=await repair_grounding(http,data,user,history,url,grounding_flags)
            data['_local_speech_issues']=speech.issues(data['reply'],user,history)
            if os.getenv('SPEECH_REVIEW_ENABLED','0') == '1' and not data.get('_grounding_repair') and data['response_plan']['mode']=='text':
                revised = await review_speech(http, data, user, history, body['messages'][0]['content'], url)
                if revised['reply'] != data['reply']:
                    revised['response_plan'] = {**data['response_plan'],'segments':[revised['reply']]}
                return revised
            return data
        except (ValueError,KeyError,TypeError,AssertionError,IndexError) as exc:
            log.warning('模型响应结构无效：%s: %s，自动重试=%s',type(exc).__name__,str(exc)[:220],attempt+1<attempts)
            if force_image:
                message=choice.get('message',{}) if isinstance(locals().get('choice'),dict) else {}
                raw=message.get('content','') if isinstance(message,dict) else ''
                raw=raw if isinstance(raw,str) else ''
                recovered=response_plan.partial_text(raw) or {'reply':'我想一下画面。'}
                safe=response_plan.normalize({'reply':recovered['reply'],'state':{},'send_image':False,'visual_tags':''},state,user,True)
                safe.update(_thinking_enabled=thinking,_model_format='image_plan_format_recovered',
                    _model_format_error=type(exc).__name__,_raw_model_response=raw[:8000],
                    _stage_times={'prompt_build_ms':preparation_ms,'api_ms':round((time.monotonic()-api_started)*1000),'reply_planning_ms':0})
                return safe
            if thinking:
                # Some compatible providers occasionally finish Thinking without a final
                # JSON body. One final-body repair is bounded and cannot change memory,
                # scene state, or create an image task.
                recovery_started=time.monotonic()
                recovery={**body,'thinking':{'type':'disabled'},'max_tokens':1800,
                    'messages':body['messages']+[{'role':'user','content':'上一条最终正文结构未完成。只补充最终回复JSON {"reply":"正文"}，保持当前讨论依据和口吻，不输出思考过程、state、记忆或绘图字段。'}]}
                recovery.pop('reasoning_effort',None)
                async with http.post(url,json=recovery,headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']},timeout=aiohttp.ClientTimeout(total=45)) as repaired:
                    repaired.raise_for_status();value=await repaired.json()
                payload=response_plan.decode(value['choices'][0]['message']['content'],False)
                safe=response_plan.normalize({'reply':payload.get('reply',''),'state':{},'send_image':False,'visual_tags':'',
                    'dialogue_update':payload.get('dialogue_update',{})},state,user,False)
                safe.update(_thinking_enabled=True,_thinking_stage=depth['stage'],_text_only_recovery=True,
                    _model_format='thinking_final_body_recovered',image_value={'veto':True},
                    _stage_times={'prompt_build_ms':preparation_ms,'api_ms':round((recovery_started-api_started)*1000),
                                  'final_body_recovery_ms':round((time.monotonic()-recovery_started)*1000),'reply_planning_ms':0})
                return safe
            if attempt+1>=attempts:
                error=ValueError('模型回复格式未完成，已停止自动重试；可以稍后再试')
                message=choice.get('message',{}) if isinstance(locals().get('choice'),dict) else {}
                raw=message.get('content','') if isinstance(message,dict) else ''
                error.model_response=raw[:8000] if isinstance(raw,str) else ''
                raise error from None
            body['messages'].append({'role': 'user', 'content': '请重新输出符合上述字段要求的完整 JSON。'})


async def repair_grounding(http,data,user,history,url,issues):
    """One selective repair only for unsupported causal detail, never a per-turn audit."""
    result=dict(data)
    started=time.monotonic()
    result['_grounding_repair']={'issues':issues,'draft':data['reply'],'applied':False}
    body={'model':os.getenv('DEEPSEEK_MODEL','deepseek-flash'),'thinking':{'type':'disabled'},'response_format':{'type':'json_object'},'max_tokens':400,
          'messages':[{'role':'system','content':'只修正虚拟角色中文聊天正文，输出JSON {"reply":"正文"}。保持冷淡克制的口吻。根据用户资料给自己的反应，不提问、不嘘寒问暖。若问题是重要事件只回嗯，补成一句具体而简短的态度，不复述一段。若问题是未提供原因，删去未给出的评审、导师、资源、名额或当场表现，不补另一种猜测，原因未知就承认未知。不把短回复强行变长。'},
                      {'role':'user','content':json.dumps({'recent_user_messages':[m['content'] for m in history[-6:] if m.get('role')=='user'],'message':user,'draft':data['reply'],'issues':issues},ensure_ascii=False)}]}
    try:
        async with http.post(url,json=body,headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']}) as response:
            response.raise_for_status();raw=await response.json()
        reply=json.loads(raw['choices'][0]['message']['content']).get('reply','')
        import social_reasoning
        if not isinstance(reply,str) or not reply.strip() or social_reasoning.grounding_issues(reply,user,history):
            reply='具体缘由我还不清楚。'
        result['reply']=reply
        result['response_plan']={**result['response_plan'],'segments':[reply]}
        result['_grounding_repair']['applied']=True
    except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError,TypeError,IndexError):
        result['reply']='具体缘由我还不清楚。'
        result['response_plan']={**result['response_plan'],'segments':[result['reply']]}
        result['_grounding_repair']['fallback']=True
    result.setdefault('_stage_times',{})['grounding_repair_ms']=round((time.monotonic()-started)*1000)
    return result


async def review_speech(http, data, user, history, system, url):
    """At most one selective rewrite of reply only; never mutate state, memory or images."""
    flags = speech.issues(data['reply'], user, history)
    if not flags:
        return data
    result = dict(data)
    result['_speech_review'] = {'issues':flags, 'revision':'failed'}
    try:
        body = dict(model=os.getenv('DEEPSEEK_MODEL','deepseek-flash'),
            messages=[{'role':'system','content':system + '\n这次只修改聊天文本。只输出JSON对象 {"reply":"修改后的回复"}。保留原意、角色态度、事实和已给定的图片安排，不添加经历或新问题，不输出其他字段。'},
                      *dialogue.api_history(history),
                      {'role':'user','content':json.dumps({'message':user,'draft':data['reply'],'style_issues':flags},ensure_ascii=False)}],
            response_format={'type':'json_object'},max_tokens=800,thinking={'type':'disabled'})
        async with http.post(url,json=body,headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']},
                             timeout=aiohttp.ClientTimeout(total=12)) as response:
            response.raise_for_status()
            revised=await response.json()
        choice = revised['choices'][0]
        if choice.get('finish_reason') == 'length':
            return result
        reply=json.loads(choice['message']['content']).get('reply')
        if not isinstance(reply,str) or not 0<len(reply.strip())<=4000:
            return result
        if speech.issues(reply,user,history):
            result['_speech_review']['revision']='rejected'
            return result
        result['reply']=reply.strip()
        result['_speech_review']['revision']='applied'
        return result
    except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,TypeError,KeyError,IndexError,AttributeError):
        log.info('口吻复核未完成，保留原回复')
        return result


class Bot:
    def __init__(self, http):
        self.http = http
        self.comfy = Comfy(http)
        (ROOT / 'data').mkdir(exist_ok=True)
        self.store = Store(ROOT / 'data' / 'sessions.db')
        self.users, self.groups = ids('ALLOWED_USERS'), ids('ALLOWED_GROUPS')
        self.pending, self.locks, self.cooldowns = {}, {}, {}
        self.seen = {}
        self.tasks = set()
        self.ws = None
        self.connected = asyncio.Event()
        self.image_tasks = {}
        self.image_queues = {}
        self.image_active = {}
        self.image_planning = {}
        self.image_epochs = {}
        self.image_slots = image_director.PrioritySlots()
        self.activity = {}
        self.last_reaction = {}
        self.typing_users = set()
        self.inbox = {}
        self.inbox_workers = {}
        self.context = ContextEngine(ROOT)
        self.scheduler = ReplyScheduler()
        self.comfy.gpu.internal_busy=lambda:bool(self.image_load() or self.image_tasks or self.inbox_workers)
        self.started_at=time.time()

    def delivery_metric(self,event,ticket,**fields):
        if ticket is None:
            return
        try:
            item={'event':event,'at':now().isoformat(timespec='seconds'),'conversation':hashlib.sha256(ticket.key.encode()).hexdigest()[:12],
                  'version':ticket.version,'packets':ticket.packets,'elapsed_seconds':round(time.monotonic()-ticket.started,3),**fields}
            with (ROOT/'data'/'delivery_metrics.jsonl').open('a',encoding='utf-8') as archive:
                archive.write(json.dumps(item,ensure_ascii=False)+'\n')
        except OSError:
            log.warning('发送时序指标未能保存，聊天继续')

    async def call(self, action, params, ws):
        echo = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        try:
            await ws.send_json(dict(action=action, params=params, echo=echo))
            result = await asyncio.wait_for(fut, 30)
            if result.get('retcode') != 0:
                raise ImageFailure('qq_send_rejected','send',f'QQ 发送失败，retcode={result.get("retcode")}')
            return result.get("data")
        finally:
            self.pending.pop(echo, None)

    async def indicate_typing(self, event, active=True):
        if event.get('message_type') != 'private':return
        user=str(event['user_id'])
        if not active:self.typing_users.discard(user)
        if self.ws is None:return
        if active:self.typing_users.add(user)
        else:
            self.typing_users.discard(user)
            # Installed NapCat documents no stop code; never guess another event type.
            return
        try:
            await asyncio.wait_for(self.call('set_input_status', {'user_id':user,'event_type':1}, self.ws), timeout=2)
        except Exception:
            log.info('QQ 输入状态不可用，继续正常回复')

    async def send(self, event, text=None, image=None, clock=False, background=False, image_guard=None, on_dispatch=None):
        ticket = None if background else CURRENT.get()
        self.scheduler.check(ticket)
        send_started=time.monotonic()
        if clock:
            text = dialogue.time_answer(now())
        elif text:
            text = dialogue.render_time(text, now())
        if ticket and text and not clock:
            reading_before=ticket.reading_ms;typing_before=ticket.typing_ms
            remaining = self.scheduler.remaining(ticket,len(text),ticket.packets)
            planned_reading=ticket.reading_ms-reading_before
            if remaining > .5:
                await self.indicate_typing(event)
            self.delivery_metric('typing',ticket,typing_seconds=round(remaining,3))
            wait_started=time.monotonic()
            try:
                await self.scheduler.wait(ticket,remaining)
            finally:
                elapsed_wait=(time.monotonic()-wait_started)*1000
                ticket.reading_ms=reading_before+min(planned_reading,elapsed_wait)
                ticket.typing_ms=typing_before+max(0,elapsed_wait-planned_reading)
        elif text and not clock and reply_timing.get():
            started, read_length = reply_timing.get()
            await asyncio.sleep(response_delay(read_length,len(text),time.monotonic()-started))
        send_ready_started=time.monotonic()
        await asyncio.wait_for(self.connected.wait(),45)
        self.scheduler.check(ticket)
        if not text and not image:
            return
        msg=[]
        if text:
            msg.append({'type':'text','data':{'text':text}})
        if image:
            msg.append({'type':'image','data':{'file':'base64://'+base64.b64encode(image).decode()}})
        group=event['message_type']=='group'
        key=f"{event['message_type']}:{event.get('group_id',0)}:{event['user_id']}"
        params={'group_id' if group else 'user_id':event['group_id'] if group else event['user_id'],'message':msg}
        async with self.scheduler.lock(key):
            self.scheduler.check(ticket)
            if image_guard and not image_guard():
                raise Superseded()
            if ticket:
                ticket.in_flight=True
            if on_dispatch:
                on_dispatch()
            dispatch_started=time.monotonic()
            try:
                acknowledgement=await self.call('send_group_msg' if group else 'send_private_msg',params,self.ws)
                if image and (not isinstance(acknowledgement,dict) or not acknowledgement.get('message_id')):
                    raise ImageFailure('qq_ack_missing','send','QQ accepted the request without a message identifier; do not resend')
                if ticket:
                    ticket.packets += 1
                    self.delivery_metric('sent',ticket,kind='text' if text else 'image',collection_ms=ticket.collection_ms,reading_ms=round(ticket.reading_ms),model_ms=round(ticket.model_ms),typing_ms=round(ticket.typing_ms),send_delay_ms=round((time.monotonic()-send_ready_started)*1000),total_elapsed_ms=round((time.monotonic()-ticket.started)*1000),response_length=len(text or ''),message_count=ticket.message_count,reply_type=ticket.reply_type,segment_count=ticket.segment_count,cancelled=False,sent=True)
                return acknowledgement
            finally:
                if ticket:
                    ticket.in_flight=False

    def accept(self, e):
        if e.get('post_type') != 'message' or e.get('user_id') not in self.users:
            return None
        if e.get('user_id') == e.get('self_id'):
            return None
        kind = e.get('message_type')
        if kind not in ('private', 'group'):
            return None
        # Segment arrays avoid treating quoted CQ codes as real mentions.
        segments = e.get('message', [])
        if not isinstance(segments, list):
            return None
        text = ''.join(str(s.get('data', {}).get('text', '')) for s in segments if s.get('type') == 'text').strip()
        if kind == 'group':
            if e.get('group_id') not in self.groups:
                return None
            mentioned = any(s.get('type') == 'at' and str(s.get('data', {}).get('qq')) == str(e.get('self_id')) for s in segments)
            if text == '/bot' or text.startswith('/bot '):
                text = text[4:].strip()
            elif not mentioned:
                return None
        markers = media_input.describe_segments(segments)
        if markers:
            text = (text+'\n'+'\n'.join(markers)).strip()
        if not text or len(text) > 6000:
            return None
        key = f'{kind}:{e.get("group_id", 0)}:{e["user_id"]}'
        message_id=e.get('message_id')
        dedup = f'{key}:{message_id}' if message_id is not None else None
        t = time.monotonic()
        self.seen = {k: v for k, v in self.seen.items() if t-v < 600}
        if dedup is not None and dedup in self.seen:
            return None
        if dedup is not None:
            self.seen[dedup] = t
        return key, text

    async def warm_image_engine(self):
        try:
            await services.ensure_comfy(self.http,self.comfy.url)
            if self.comfy.template is None:await self.comfy.check()
        except Exception as exc:log.warning('绘图预热失败：%s',type(exc).__name__)

    def enqueue(self, event, key, text):
        history=self.store.get(key).get('history',[])
        explicit_request=image_requested(text,history)
        instruction=image_director.image_instruction(text,history)
        if explicit_request and not any(not t.done() for t in getattr(self,'image_warmups',[])):
            warm=asyncio.create_task(self.warm_image_engine())
            self.image_warmups=[warm];self.tasks.add(warm);warm.add_done_callback(self.tasks.discard)
        event={**event,'_queued_at':time.monotonic(),'_explicit_image_request':explicit_request}
        self.scheduler.begin(key,len(text))
        if text in ('/取消','取消','停止回复','别回复了'):
            self.inbox.setdefault(key,[]).clear()
        if instruction in ('cancel','amend'):
            self.image_epochs[key]=self.image_epochs.get(key,0)+1
            for planning_key, planning_task in list(self.image_planning.values()):
                if planning_key==key:planning_task.cancel()
            image_task=self.image_active.get(key) or self.image_tasks.get(key)
            if image_task and not image_task.done() and instruction=='amend' and not text.startswith('/'):
                event={**event,'_image_amend':True}
            for item in self.image_queues.pop(key,[]):
                item['job'].record('cancelled',reason='user_cancel_or_amend')
            if image_task and not image_task.done():
                image_task.cancel()
        self.activity[key] = self.activity.get(key, 0) + 1
        self.inbox.setdefault(key, []).append((event, text))
        existing = self.inbox_workers.get(key)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self.drain_inbox(key))
        self.inbox_workers[key] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain_inbox(self, key):
        try:
            while self.inbox.get(key):
                first = time.monotonic()
                # Collect short successive messages, but do not wait indefinitely.
                if not self.inbox[key][0][1].startswith('/') and not self.inbox[key][0][0].get('_explicit_image_request'):
                    while True:
                        size = len(self.inbox[key])
                        timing=preferences.load().get('reply_timing',{})
                        window=.35 if speech.plan(self.inbox[key][-1][1],[])['mode'] in ('ack','exit') else .65
                        if size>=2:
                            gap=self.inbox[key][-1][0].get('_queued_at',first)-self.inbox[key][-2][0].get('_queued_at',first)
                            window=max(.35,min(.85,gap*1.4))
                        await asyncio.sleep(max(timing.get('collection_min',.35),min(timing.get('collection_max',.85),window)))
                        if len(self.inbox[key]) == size or time.monotonic()-first >= 2:
                            break
                pending = self.inbox[key]
                event, text = pending.pop(0)
                event = dict(event)
                segments = list(event.get('message',[]))
                message_count=1
                if not text.startswith('/') and not event.get('_explicit_image_request'):
                    while pending and not pending[0][1].startswith('/') and not pending[0][0].get('_explicit_image_request') and len(text)+len(pending[0][1])+1 <= 6000:
                        following, extra = pending.pop(0)
                        segments.extend(following.get('message',[]))
                        text += '\n' + extra
                        message_count+=1
                event['message'] = segments
                ticket=self.scheduler.current.get(key)
                if ticket:
                    ticket.read_length=len(text)
                    ticket.message_count=message_count
                    ticket.collection_ms=round((time.monotonic()-first)*1000)
                    self.scheduler.timing=__import__('reply_scheduler').timing_config(preferences.load().get('reply_timing'))
                ticket.pace='fast' if speech.plan(text,[])['mode'] in ('ack','exit') or dialogue.time_question(text) or re.search(r'救命|急救|紧急',text) else ('complex' if speech.plan(text,[])['mode']=='help' else 'normal')
                self.delivery_metric('collected',ticket,collection_seconds=round(time.monotonic()-first,3))
                accepted = await self.handle(event,key,text,ticket=ticket)
                if accepted is False and ticket and not ticket.packets and not text.startswith('/') and pending and not pending[0][1].startswith('/') and not re.search(r'^(?:取消|停止回复|别回复了)|换个?话题|先不聊这个|别说这个',pending[0][1]):
                    # Rebuild one reply from the interrupted input plus the new supplement.
                    pending.insert(0,(event,text))
        finally:
            self.inbox_workers.pop(key, None)
            if not self.inbox.get(key):
                self.inbox.pop(key, None)

    async def handle(self, event, key, text, ticket=None):
        self.activity[key]=self.activity.get(key,0)+1
        ticket=ticket or self.scheduler.begin(key,len(text))
        token=CURRENT.set(ticket)
        reply_timing.set((ticket.started,len(text)))
        try:
            async with self.locks.setdefault(key,asyncio.Lock()):
                self.scheduler.check(ticket)
                await self.scheduler.run(ticket,self.respond(event,key,text))
            return True
        except Superseded:
            image_job=event.get('_pending_image_job')
            if image_job and image_job.data['status']=='received':image_job.record('cancelled',reason='reply_superseded_before_visual_plan')
            self.reconcile_unsent(key,ticket)
            self.delivery_metric('superseded',ticket,cancelled=True,sent=bool(ticket.packets))
            log.info('丢弃过期回复：会话版本 %s',ticket.version)
            return False
        except Exception as exc:
            image_job=event.get('_pending_image_job')
            if image_job and image_job.data['status']=='received':
                image_job.record('failed',reason='chat_model_or_context_failed',error=type(exc).__name__,detail=str(exc)[:400],model_response=getattr(exc,'model_response',''))
                self.image_decision_record(key,{'image_task_id':image_job.id,'status':'failed','reason':'chat_model_or_context_failed'})
            log.error('处理失败 (%s): %s',type(exc).__name__,str(exc)[:1800])
            try:
                self.scheduler.check(ticket)
                await self.send(event,'这次没完成，可以稍后再试。')
            except (Exception,asyncio.CancelledError):
                pass
            return True
        finally:
            try:await self.indicate_typing(event,False)
            except Exception:pass
            CURRENT.reset(token)

    def reconcile_unsent(self,key,ticket):
        from datetime import timedelta
        session=self.store.get(key);plan=session.get('delivery_plan',{})
        if plan.get('version')!=ticket.version:return
        essential=[]
        for segment in plan.get('segments',[]):
            if segment['status']=='queued':
                segment['status']='cancelled'
                if segment.get('priority')=='essential':essential.append(segment['text'])
        if essential:
            session['pending_essential']={'segments':essential,'source':plan.get('source',''),'expires_at':(now()+timedelta(minutes=5)).isoformat()}
        self.store.save(key,session)

    async def respond(self, e, key, text):
        session = self.store.get(key)
        if text.startswith('/') and CURRENT.get():CURRENT.get().pace='fast'
        received_at = now().isoformat(timespec='seconds')
        if speech.EXIT.search(text):
            from datetime import timedelta
            session['busy_until']=(now()+timedelta(hours=2)).isoformat()
        elif not text.startswith('/'):
            session.pop('busy_until',None)
        session['last_user_at']=received_at
        self.store.save(key,session)
        import social_reasoning
        stopped=social_reasoning.boundary_update(session,text,now())
        if stopped:session.pop('pending_essential',None)
        if stopped and not text.startswith('/') and image_director.image_instruction(text)!='cancel' and not image_requested(text):
            acknowledgement='好，这个先不聊。'
            session['history']=(session['history']+[{'role':'user','content':text,'at':received_at}])[-20:]
            self.store.save(key,session)
            await self.send(e,acknowledgement)
            session['history']=(session['history']+[{'role':'assistant','content':acknowledgement,'at':now().isoformat()}])[-20:]
            self.store.save(key,session)
            return
        self.store.save(key,session)
        command, _, arg = text.partition(' ')
        arg = arg.strip()
        if text in ('/取消','取消','停止回复','别回复了') or image_director.image_instruction(text)=='cancel':
            if text!='别回复了':
                await self.send(e,'好，先停下。')
            return
        if command in ('/帮助', '/help', '/bot'):
            return await self.send(e, HELP)
        if command=='/档案':
            if e.get('message_type')!='private':
                return await self.send(e,'个人档案请在私聊里查看。')
            facts=self.context.life.facts(str(e['user_id']))
            return await self.send(e,'\n'.join(f"{x['kind']}：{x['value']}" for x in facts) or '还没有需要记下的基本信息。只记录你明确说过的内容。')
        if command=='/更正':
            if e.get('message_type')!='private' or ' => ' not in arg:
                return await self.send(e,'请在私聊用 /更正 原条目 => 新条目，更正自己的档案。')
            old,new=arg.split(' => ',1)
            changed=self.context.life.correct(str(e['user_id']),old.strip(),new.strip(),now())
            if changed:
                session['history']=[m for m in session['history'] if old.strip() not in m.get('content','')]
                self.store.save(key,session)
            return await self.send(e,'已更正，旧条目保留为带日期的历史。' if changed else '没有找到完全相同的原条目，先用 /档案 查看。')
        if command=='/主动分享':
            if e.get('message_type')!='private' or not is_owner(e['user_id']):
                return await self.send(e,'请由主人在私聊调整。')
            if arg not in ('开','关'):
                return await self.send(e,'用 /主动分享 开 或 /主动分享 关。开启后空闲时偶尔分享虚拟生活插画。')
            preferences.save({'proactive_enabled':arg=='开'})
            return await self.send(e,'已开启空闲时的生活插画分享。' if arg=='开' else '已关闭主动分享。')
        if command in ('/日程', '/纪念日', '/忘记', '/忘记纪念日', '/相处', '/随手图'):
            if e.get('message_type') != 'private':
                return await self.send(e, '这些个人设置请在私聊里使用。')
            user = str(e['user_id'])
            if command in ('/相处', '/随手图') and not is_owner(user):
                return await self.send(e, '全局相处与发图设置只由主人账号调整。')
            if command == '/日程':
                routine_user = user+':'+character_key(session['state']['character'])
                await self.context.routine.ensure(self.http, routine_user, now(), session['state']['character'])
                items = self.context.routine.agenda(routine_user, now())
                return await self.send(e, '今天的虚拟日程（计划可以调整）：\n'+'\n'.join(f'{a:02d}—{b:02d}时 {activity}' for a,b,activity in items))
            if command == '/纪念日':
                try:
                    if arg:
                        name, day = arg.rsplit(' ', 1)
                        self.context.companion.anniversary(user, name, day)
                    dates = self.context.companion.anniversary(user)
                except ValueError:
                    return await self.send(e, '用 /纪念日 名称 MM-DD 登记，例如 /纪念日 第一次聊天 10-30。')
                return await self.send(e, '\n'.join(f'{day} {name}' for name,day in dates) or '还没有登记纪念日。用 /纪念日 名称 MM-DD 添加。')
            if command == '/忘记纪念日':
                count = self.context.companion.forget(user, arg)
                return await self.send(e, '已移除这项纪念日。' if count else '没有找到同名纪念日，请填写完整名称。')
            if command == '/忘记':
                if not arg:
                    return await self.send(e, '先用 /档案 查看，再用 /忘记 完整条目内容 移除一项。')
                count = self.context.life.forget(user, arg)
                # Remove the forgotten phrase from short conversational context as well.
                session['history'] = [m for m in session['history'] if arg not in m.get('content', '')]
                self.store.save(key, session)
                return await self.send(e, '已从档案和当前聊天上下文移除这项内容。' if count else '档案没有找到完全相同的条目；已移除当前上下文里的对应内容。')
            if command == '/相处':
                modes = {'慢慢来':'slow', '朋友':'friends', '恋人':'partner'}
                if arg not in modes:
                    return await self.send(e, '用 /相处 慢慢来、/相处 朋友 或 /相处 恋人 调整。')
                preferences.save({'romance_mode':modes[arg]})
                return await self.send(e, '好，就按这个节奏相处。')
            if arg not in ('开', '关'):
                return await self.send(e, '用 /随手图 开 或 /随手图 关；只在聊天中偶尔分享，明确要图不受此开关影响。')
            preferences.save({'spontaneous_images':arg == '开'})
            return await self.send(e, '已开启聊天中的偶尔分享。' if arg == '开' else '已关闭偶尔分享，仍可随时明确要图。')
        if command == '/笔记':
            if e.get('message_type') != 'private' or not is_owner(e['user_id']):
                return await self.send(e, '笔记只在私聊里读取，避免把你的内容发到群里。')
            notes = await read_notes('search' if arg else 'list', arg)
            return await self.send(e, format_notes(notes))
        if image_progress.is_query(text,session,now()):
            status=image_progress.snapshot(ROOT,session,key,now(),self.started_at)
            if CURRENT.get():CURRENT.get().pace='fast'
            await self.send(e,status['text'])
            # A background image may finish while QQ acknowledges this text.
            session=self.store.get(key)
            session['history']=(session['history']+[{'role':'user','content':text,'at':received_at},
                {'role':'assistant','content':status['text'],'at':now().isoformat()}])[-20:]
            session['last_image_progress_at']=now().isoformat()
            self.store.save(key,session)
            return
        if dialogue.time_question(text):
            await self.send(e, clock=True)
            session['history'] = (session['history'] + [
                {'role': 'user', 'content': text, 'at': received_at},
                {'role': 'assistant', 'content': dialogue.time_answer(now()), 'at': now().isoformat(timespec='seconds')}])[-20:]
            self.store.save(key, session)
            log.info('时间询问由系统时钟直接回答，时区=%s', now().tzinfo)
            return
        if command == '/重置':
            self.store.save(key, dict(state=DEFAULT.copy(), history=[]))
            return await self.send(e, '已恢复默认角色并清空聊天记忆。')
        if command == '/状态':
            s = session['state']
            return await self.send(e, f'角色：{s["character"]}\n服装：{s["clothing"]}\n场景：{s["location"]}\n现实时间：{now():%Y-%m-%d %H}时')
        changes = {'/角色': 'character', '/换装': 'clothing', '/地点': 'location'}
        if command in changes:
            if not arg:
                return await self.send(e, '请在指令后加空格，再填写描述。')
            if len(arg) > 2000:
                return await self.send(e, '设定请控制在 2000 字以内。')
            session['state'][changes[command]] = arg
            if command=='/角色':
                session.pop('scene_hair',None)
                session.pop('visual_cache',None)
            self.store.save(key, session)
            return await self.send(e, '设定已更新。发 /拍照 就能看看现在的样子。')
        if session['state'] == OLD_DEFAULT:
            session['state'] = DEFAULT.copy()
            self.store.save(key, session)
        force = command == '/拍照'
        explicit = image_requested(text,session.get('history',[])) or bool(e.get('_image_amend'))
        if explicit:
            task_id=uuid.uuid4().hex
            e['_pending_image_job']=image_director.Job(ROOT/'data'/'image_jobs',{'state':copy.deepcopy(session['state'])},
                {'kind':'explicit','style':'life','priority':0,'image_task_id':task_id},'',self.activity.get(key,0))
            detection='current_text' if image_requested(text) else 'recent_visual_context'
            e['_pending_image_job'].record('received',reason='explicit_user_request',request_detection=detection)
            e['_pending_image_job'].data['conversation_id']=hashlib.sha256(key.encode()).hexdigest()[:12]
            e['_pending_image_job'].save()
            session['last_image_job_id']=task_id
            self.store.save(key,session)
            self.image_decision_record(key,{'image_task_id':task_id,'status':'received','reason':'explicit_user_request','request_detection':detection})
        request = ('请生成一张你现在的照片。' + arg) if force else text
        if e.get('_image_amend'):
            request='修改上一张图片请求：'+session.get('last_image_request','')+'；本次修改：'+text
        previous=next((m.get('content','') for m in reversed(session['history']) if m.get('role')=='assistant'),None)
        if e.get('message_type')=='private' and re.fullmatch(r'[嗯哦噢]+[。！!，,～~\s]*',text) and previous is not None and speech.question_count(previous)==0:
            # Acknowledgement after a completed statement may end naturally; no model bill.
            session['history']=(session['history']+[{'role':'user','content':text,'at':received_at}])[-20:]
            self.store.save(key,session)
            return
        context_started=time.monotonic()
        depth=dialogue_depth.plan(session,text,session.get('history',[]),now(),preferences.load().get('thinking_after_substantive_turns',4))
        extra=await self.context.build(self.http,str(e['user_id']),text,now(),e.get('message_type')=='private',character=session['state']['character'],visual_requested=explicit)
        visual_target=visual_request_context(text,session.get('history',[])) if explicit else {}
        if explicit:extra+='\n本轮明确索图目标：'+json.dumps(visual_target,ensure_ascii=False)
        extra += '\n'+response_plan.life_context(session,now())
        pending=session.get('pending_essential',{})
        try:
            if pending and datetime.fromisoformat(pending['expires_at'])>now():
                extra+='\n上一轮尚未发送的重要内容（不是已说过的话）：'+json.dumps(pending,ensure_ascii=False)+'。根据用户新消息重组，输出carry_resolution:included/irrelevant/deferred；不可把旧内容原样全发完。'
            else:session.pop('pending_essential',None)
        except (KeyError,ValueError,TypeError):session.pop('pending_essential',None)
        extra += '\n'+image_director.scene_context(session)+'\n'+social_reasoning.context(session)
        self.delivery_metric('context_ready',CURRENT.get(),context_preparation_ms=round((time.monotonic()-context_started)*1000))
        if session.get('last_image_context'):
            extra += '\n最近成功发出的虚拟插画（用户可能在评价画面，不自动重画）：'+json.dumps(session['last_image_context'],ensure_ascii=False)[:1800]
        if session.get('last_image_job_id') or image_progress.recent_visual(session.get('history',[]),now()):
            extra+='\n图片任务的真实记录：'+json.dumps(image_progress.snapshot(ROOT,session,key,now(),self.started_at),ensure_ascii=False)
            extra+='。只按这个记录描述进度；没有任务时不说在画、快了或已经发图；历史承诺不是任务已创建的证据。'
        observation=None
        if media_input.describe_segments(e.get('message',[])):
            observation=await media_input.observe(self.http,e.get('message',[]))
            extra += '\n'+media_input.evidence_context(observation)
        if observation and observation['status'] not in ('recognized','attached') and not any(seg.get('type')=='text' and str(seg.get('data',{}).get('text','')).strip() for seg in e.get('message',[])):
            if observation.get('images'):
                await self.send(e,'这张图片我还没识别出来。')
            session['history']=(session['history']+[{'role':'user','content':request,'at':received_at},{'role':'assistant','content':'[附件未识别，已如实说明]','at':now().isoformat(timespec='seconds')}])[-20:]
            session['last_media']={'at':received_at,**observation}
            self.store.save(key,session)
            return
        fixed_visual = cache_context(session)
        if fixed_visual:
            extra += '\n'+fixed_visual+'\n仅在同一套服装或同一地点时沿用；用户明确换装、换地点时重新描述对应部分。'
        tone = self.context.life.mood(str(e['user_id']),{},now()) if e.get('message_type')=='private' else '自然平稳，跟随用户话题。'
        model_started=time.monotonic()
        data = await deepseek(self.http, session['state'], session['history'], request, explicit, context=extra,media=observation,depth=depth)
        if explicit and data.get('_model_format_error'):
            e['_pending_image_job'].data.update(main_model_format_error=data['_model_format_error'],main_model_response=data.get('_raw_model_response',''))
            e['_pending_image_job'].save()
        data['_explicit_image_request']=explicit
        if explicit:
            data['_image_purpose']=visual_target['purpose']
            data['_visual_request_context']='本轮明确索图目标：'+json.dumps(visual_target,ensure_ascii=False)+'\n最近的视觉话题：'+json.dumps(session.get('history',[])[-4:],ensure_ascii=False)
        image_director.transition(session,data,text,now())
        self.delivery_metric('model_complete',CURRENT.get(),model_seconds=round(time.monotonic()-model_started,3),thinking=bool(data.get('_thinking_enabled',depth['thinking'])),thinking_stage=depth['stage'],discussion_turns=depth['state'].get('substantive_turns',0),**data.get('_stage_times',{}))
        self.scheduler.check(CURRENT.get())
        if observation:
            observation=media_input.finalize(observation,data.get('media_analysis'))
            if observation['status']=='unclear':
                data['response_plan']={'intent':'casual_chat','mode':'text','should_reply':True,'segments':['这张我没看清。']}
                data['reply']='这张我没看清。'
        if data.get('_local_speech_issues'):
            self.delivery_metric('speech_flags',CURRENT.get(),issue_count=len(data['_local_speech_issues']))
        plan=response_plan.parse(data)
        ticket=CURRENT.get()
        if ticket:
            ticket.model_ms=round((time.monotonic()-model_started)*1000)
            ticket.reply_type=plan['intent']
            ticket.segment_count=len(plan['segments'])
            if plan['intent']=='emotion':ticket.pace='emotion'
            elif plan['intent'] in ('acknowledge','closing'):ticket.pace='fast'
        drawing_tags=assemble(session,data)
        committed=False
        def commit(spoken=None):
            nonlocal committed
            if not committed:
                session['state']=data['state']
                if data.get('_scene_hair'):
                    session['scene_hair']=data['_scene_hair']
                if data.get('_scene_changes'):
                    session['scene_changes']=(session.get('scene_changes',[])+[{'at':now().isoformat(),'changes':data['_scene_changes']}])[-20:]
                response_plan.update_life(session,data,now())
                social_reasoning.remember(session,data,now())
                session['history']=(session['history']+[{'role':'user','content':request,'at':received_at}])[-20:]
                if observation:
                    session['last_media']={'at':received_at,**observation}
                if e.get('message_type')=='private':
                    self.context.companion.touch(str(e['user_id']),session['state']['character'],now())
                    self.context.life.record(str(e['user_id']),data.get('memory_updates',[]),text,now())
                    self.context.life.mood(str(e['user_id']),data.get('affect',{}),now())
                committed=True
            if spoken:
                if data.get('carry_resolution') in ('included','irrelevant'):session.pop('pending_essential',None)
                session['history']=(session['history']+[{'role':'assistant','content':dialogue.render_time(spoken,now()),'at':now().isoformat(timespec='seconds')}])[-20:]
            self.store.save(key,session)
        # Persist the planned scene first, then let GPU work run during text pacing.
        commit()
        await self.start_drawing(e,key,request if e.get('_image_amend') else text,session,data,plan,explicit)
        plan=image_progress.guard_blocked_reply(plan,data)
        if ticket:ticket.segment_count=len(plan['segments'])
        if plan['mode']=='silent':
            commit()
            if not explicit:
                return
        if plan['mode']=='reaction':
            if await self.send_reaction(e,key,session,data.get('reaction','none'),text):
                commit('[实际发送了一张表情图片]')
            else:
                commit()
            if not explicit:
                return
        ticket=CURRENT.get()
        session['delivery_plan']={'version':ticket.version if ticket else 0,'source':request,'segments':[{'index':i,'text':segment,'priority':plan.get('segment_priority',['optional']*len(plan['segments']))[i],'status':'queued'} for i,segment in enumerate(plan['segments'])]}
        self.store.save(key,session)
        for index,segment in enumerate(plan['segments']):
            self.scheduler.check(CURRENT.get())
            acknowledgement=await self.send(e,segment)
            session['delivery_plan']['segments'][index].update(status='sent',qq_result=acknowledgement,at=now().isoformat())
            session['delivery_receipts']=(session.get('delivery_receipts',[])+[{'version':ticket.version if ticket else 0,'segment':index,'state':'sent','qq_result':acknowledgement,'at':now().isoformat()}])[-50:]
            commit(segment)
            await self.indicate_typing(e,False)
        if not committed:
            commit()
        if plan['mode']=='text' and plan['segments']:
            dialogue_depth.finish(session,text,'\n'.join(plan['segments']),data,depth,now())
            self.store.save(key,session)
        if data.get('_image_failure'):
            notice=self.image_failure_notice(data['_image_failure'])
            await self.send(e,notice);commit(notice)
        elif not data.get('_drawing_started'):
            await self.send_reaction(e,key,session,data.get('reaction','none'),text)

    async def start_drawing(self,e,key,text,session,data,plan,explicit):
        import social_reasoning
        # Explicit requests work regardless of auto mode; otherwise images are opt-in.
        auto = os.getenv('IMAGE_MODE', 'off') == 'auto' and not data.get('_text_only_recovery') and not social_reasoning.boundary_blocks(session,text,now())
        settings=preferences.load()
        if not explicit and auto and settings.get('spontaneous_images',True):
            import image_candidates
            candidate_data=data
            candidate=image_candidates.conversation(key,session,text,data,now())
            if candidate is None and e.get('message_type')=='private' and not data.get('image_value',{}).get('veto') and data.get('_thinking_stage') not in ('B','C') and plan['intent'] in ('casual_chat','share','question') and speech.plan(text,session.get('history',[]))['mode']!='help':
                routine_user=str(e['user_id'])+':'+character_key(session['state']['character'])
                routine_event=self.context.routine.current_event(routine_user,now())
                candidate=image_candidates.life(key,session,now(),routine_event)
                if candidate:
                    # Chat visual fields may describe a different proposed activity.
                    candidate_data={**data,'visual_tags':'','visual_scene':{}}
            if candidate:
                self.store.save(key,session)
                task=asyncio.create_task(self.prepare_candidate(e,key,candidate,settings,candidate_data))
                self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                data['_drawing_started']=True
                return
        decision=image_director.decide(session,text,data,now(),settings['drawing'],auto and settings.get('spontaneous_images',True))
        pending_job=e.get('_pending_image_job')
        decision.setdefault('image_task_id',pending_job.id if pending_job else uuid.uuid4().hex)
        if explicit and decision['reason']!='user_declined':
            decision.update(allowed=True,kind='explicit',priority=0,reason='explicit_user_request')
        if pending_job:pending_job.data['decision']=copy.deepcopy(decision)
        should_draw=decision['allowed']
        self.image_decision_record(key,decision)
        log.info('图片判定：明确请求=%s，模型建议=%s，自动模式=%s，决定生图=%s', explicit, data['send_image'], auto, should_draw)
        if not should_draw:
            if pending_job:pending_job.record('cancelled',reason=decision.get('reason','user_declined'))
            if not explicit and plan.get('intent')=='image_request':
                self.delivery_metric('image_promise_blocked',CURRENT.get(),reason=decision.get('reason','not_proposed'))
            return
        if not complete_from_cache(session,data) and not image_concept.needs_plan(data):
            launched=self.launch_image(e,key,text,session,data,decision,settings,explicit,job=pending_job)
            if launched:data['_drawing_started']=True
            elif explicit:data['_image_failure']=data.get('_image_failure','queue_full')
            return
        # Visual repair is independent of the text reply and its cancellation ticket.
        job=pending_job or image_director.Job(ROOT/'data'/'image_jobs',{'state':copy.deepcopy(session['state'])},decision,'',self.activity.get(key,0))
        job.data['creative_brief']=copy.deepcopy(data.get('creative_brief'))
        job.record('planning',reason='creative_or_visual_completion')
        if self.image_load()>=settings['drawing'].get('queue_limit',6):
            job.record('rejected',reason='queue_full');data['_image_failure']='queue_full';return
        epoch=self.image_epochs.get(key,0)
        task=asyncio.create_task(self.prepare_image(e,key,text,copy.deepcopy(data),decision,settings,explicit,job,epoch))
        self.image_planning[job.id]=(key,task)
        task.add_done_callback(lambda done:job.record('cancelled',reason='cancel_before_planning_started') if done.cancelled() and job.data['status']=='planning' else None)
        self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        data['_drawing_started']=True

    def image_load(self):
        return sum(len(q) for q in self.image_queues.values())+len(self.image_active)+len(self.image_planning)

    async def prepare_image(self,event,key,text,data,decision,settings,explicit,job,epoch):
        token=CURRENT.set(None)
        timing_token=reply_timing.set(None)
        scene=copy.deepcopy(data['state'])
        def valid():
            return self.store.get(key)['state']==scene and self.image_epochs.get(key,0)==epoch
        try:
            from image_planner import complete_visual
            session=self.store.get(key)
            missing=complete_from_cache(session,data)
            if missing or image_concept.needs_plan(data):
                job.record('planning',reason='visual_completion',missing_fields=missing,planning_requests=1)
                url=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com').rstrip('/')+'/chat/completions'
                visual=await complete_visual(self.http,url,scene,text,cache_context(session)+'\n'+image_concept.context(session,text,data))
                data.update(visual)
            job.data['creative_brief']=copy.deepcopy(data.get('creative_brief'));job.save()
            if not valid():raise Superseded()
            session=self.store.get(key)
            missing=complete_from_cache(session,data)
            if missing:raise ImageFailure('visual_fields_missing','planning',','.join(missing))
            if not image_concept.apply(data):raise ImageFailure('creative_fields_missing','planning','Incomplete or invalid creative_brief')
            self.image_planning.pop(job.id,None)
            launched=self.launch_image(event,key,text,session,data,decision,settings,explicit,job=job)
            if not launched and explicit:
                await self.notify_image_failure(event,key,self.image_failure_notice(data.get('_image_failure','visual_fields_missing')),valid,job)
        except (asyncio.CancelledError,Superseded):
            job.record('cancelled',reason='cancel_or_scene_changed')
        except Exception as exc:
            reason=exc.code if isinstance(exc,ImageFailure) else 'visual_planning_failed'
            job.record('failed',reason=reason,error=type(exc).__name__,detail=str(exc)[:500])
            self.image_decision_record(key,{**decision,'status':'failed','reason':reason})
            if explicit and valid():
                try:await self.notify_image_failure(event,key,self.image_failure_notice(reason),valid,job)
                except (Exception,Superseded):pass
        finally:
            self.image_planning.pop(job.id,None)
            reply_timing.reset(timing_token)
            CURRENT.reset(token)

    async def notify_image_failure(self,event,key,notice,valid,job):
        acknowledgement=await self.send(event,notice,background=True,image_guard=valid)
        async with self.locks.setdefault(key,asyncio.Lock()):
            live=self.store.get(key)
            live['history']=(live['history']+[{'role':'assistant','content':notice,'at':now().isoformat()}])[-20:]
            self.store.save(key,live)
        job.record(job.data['status'],reason=job.data.get('reason','failed'),user_notified=True,notice_result=acknowledgement)

    @staticmethod
    def image_failure_notice(reason):
        return '这张暂时排不进去，等前面的画完再试吧。' if reason=='queue_full' else '这次没能补全画面的服装、地点或动作，图片没有开始生成。'


    def launch_image(self,e,key,text,session,data,decision,settings,explicit=False,job=None):
        if self.image_load()>=settings['drawing'].get('queue_limit',6):
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'queue_full'})
            data['_image_failure']='queue_full'
            if job:job.record('rejected',reason='queue_full')
            return
        missing=complete_from_cache(session,data)
        if not image_concept.apply(data):
            data['_image_failure']='creative_fields_missing'
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'creative_fields_missing'})
            if job:job.record('failed',reason='creative_fields_missing',creative_brief=data.get('creative_brief'))
            return
        if missing:
            data['_image_failure']='visual_fields_missing'
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'visual_fields_missing','missing_fields':missing})
            if job:job.record('failed',reason='visual_fields_missing',missing_fields=missing)
            return
        assemble(session,data)
        count = picture_count(text) if explicit else 1
        data['_image_request']=text
        snapshot=image_director.freeze(session,data,decision,now(),settings['drawing'])
        snapshot['creative_brief']=copy.deepcopy(data['creative_brief'])
        snapshot['setting_tags']=scene_tags(snapshot['setting_tags'],datetime.fromisoformat(snapshot['local_time']).hour,'')
        if not snapshot['action']:
            data['_image_failure']='missing_english_action'
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'missing_english_action'});return
        snapshot['shot_plan']=shot_director.choose(snapshot,text,session.get('shot_history',[])+session.get('shot_attempts',[]))
        snapshot['camera']=snapshot['shot_plan']['tags']
        visual_key=image_director.fingerprint([snapshot['location'],snapshot['outfit'],snapshot['action'],snapshot['camera']])
        if not explicit and any(v.get('key')==visual_key for v in session.get('recent_visuals',[])[-2:]):
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'repeated_visual'});return
        prompt = visual_prompt(image_director.build_tags(snapshot),datetime.fromisoformat(snapshot['local_time']).hour,
                               '',shot_director.visible_emotion(snapshot),session['state']['character'],frozen=True)
        render_config=image_workflow.prepare(ROOT,snapshot)
        prompt=image_workflow.prompt(snapshot,render_config,prompt)
        snapshot['shot_plan']['aspect_ratio']=[render_config.get('width'),render_config.get('height')]
        if job:
            job.snapshot=copy.deepcopy(snapshot)
            job.data.update(snapshot=job.snapshot,prompt=prompt)
        else:job=image_director.Job(ROOT/'data'/'image_jobs',snapshot,decision,prompt,self.activity.get(key,0))
        job.data.update(conversation_id=hashlib.sha256(key.encode()).hexdigest()[:12],related_event_id=decision.get('event'),message_version=CURRENT.get().version if CURRENT.get() else None,image_epoch=self.image_epochs.get(key,0))
        session['last_image_job_id']=job.id
        job.record('accepted',reason=decision.get('reason','accepted'))
        job.data['candidate']=data.get('_candidate')
        caption=data.get('image_caption','')
        job.data['caption']=caption.strip()[:100] if isinstance(caption,str) else ''
        job.data['render_config']=render_config
        job.data['final_visual_scene']={k:snapshot[v] for k,v in [('identity','identity'),('outfit','outfit_tags'),('setting','setting_tags'),('action','action'),('composition','camera')]}
        job.data['initiative']=bool(e.get('_initiative'))
        job.data.update(source_message=text,model_image_plan=data.get('image_plan',{}),scene_changes=data.get('_scene_changes',[]),creative_brief=copy.deepcopy(data['creative_brief']))
        job.save()
        session['drawing_attempts']=(session.get('drawing_attempts',[])+[{'event':decision.get('event'),
            'kind':decision['kind'],'day':snapshot['local_time'][:10],'at':now().isoformat(),'job':job.id}])[-50:]
        session['last_image_request']=text or session.get('last_image_request','')
        self.store.save(key,session)
        item=dict(event=e,key=key,prompt=prompt,lines=data.get('waiting_lines',[]),scene=session['state'].copy(),activity=self.activity.get(key,0),picture_count=count,job=job)
        self.image_queues.setdefault(key,[]).append(item)
        job.record('queued',reason='bounded_priority_queue')
        self.image_decision_record(key,{**decision,'image_task_id':job.id,'status':'queued'})
        if key not in self.image_tasks or self.image_tasks[key].done():
            task=asyncio.create_task(self.image_worker(key))
            self.image_tasks[key]=task
            self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        return job

    async def image_worker(self,key):
        try:
            while self.image_queues.get(key):
                queue=self.image_queues[key]
                index=min(range(len(queue)),key=lambda i:queue[i]['job'].data['decision'].get('priority',30))
                item=queue.pop(index)
                job=item['job']
                if time.time()-job.data['created_at']>=preferences.load()['drawing']['task_ttl_seconds']:
                    job.record('expired',reason='queue_ttl')
                    if job.data['decision'].get('kind')=='explicit':
                        token=CURRENT.set(None);timing_token=reply_timing.set(None)
                        try:await self.notify_image_failure(item['event'],key,'这张排队等得太久，已经停止了。',lambda:self.store.get(key)['state']==item['scene'],job)
                        except (Exception,Superseded):pass
                        finally:CURRENT.reset(token);reply_timing.reset(timing_token)
                    continue
                render=asyncio.create_task(self.render_live(**item))
                self.image_active[key]=render
                try:await render
                finally:
                    if self.image_active.get(key) is render:self.image_active.pop(key,None)
                if asyncio.current_task().cancelling():break
        finally:
            if self.image_tasks.get(key) is asyncio.current_task():self.image_tasks.pop(key,None)
            for item in self.image_queues.pop(key,[]):
                item['job'].record('cancelled',reason='worker_stopped')

    async def send_reaction(self,event,key,session,reaction,text):
        if reaction=='none' or not preferences.load()['reactions'] or speech.plan(text,[])['mode'] in ('help','vent','closing','exit'):
            return False
        current=now()
        recent=session.get('reaction_history',[])
        if any(item.get('reaction')==reaction and (current-datetime.fromisoformat(item['at'])).total_seconds()<600 for item in recent):
            return False
        if time.monotonic()-self.last_reaction.get(key,-1e10)<120:
            return False
        files=sorted((ROOT/'assets'/'reactions').glob(reaction+'*.png'))
        if not files:
            return False
        previous=session.get('last_reaction_file')
        candidates=[p for p in files if p.name!=previous] or files
        sticker=secrets.choice(candidates)
        await self.send(event,image=sticker.read_bytes())
        self.last_reaction[key]=time.monotonic()
        session['reaction_history']=(recent+[{'reaction':reaction,'at':current.isoformat()}])[-12:]
        session['last_reaction_file']=sticker.name
        self.store.save(key,session)
        return True

    def image_decision_record(self,key,decision):
        decision={k:v for k,v in decision.items() if k!='candidate'}
        decision.setdefault('status','accepted' if decision.get('allowed') else 'rejected')
        with (ROOT/'data'/'image_decisions.jsonl').open('a',encoding='utf-8') as archive:
            archive.write(json.dumps({'at':now().isoformat(),'session':hashlib.sha256(key.encode()).hexdigest()[:12],**decision},ensure_ascii=False)+'\n')

    async def render_live(self, event, key, prompt, lines, scene, activity, picture_count=1,job=None):
        """GPU priority, quality gate and QQ sends share the chat's final delivery lock."""
        original_ticket=CURRENT.get()
        token=CURRENT.set(None)
        timing_token=reply_timing.set(None)
        cfg=preferences.load()['drawing']
        started=time.monotonic()
        remaining_ttl=max(0,cfg['task_ttl_seconds']-(time.time()-job.data['created_at'] if job else 0))
        def valid():
            live=self.store.get(key)
            task=self.image_active.get(key) or self.image_tasks.get(key)
            if job and job.data['decision'].get('kind')!='explicit':
                import social_reasoning
                candidate=job.data.get('candidate') or {}
                if social_reasoning.boundary_blocks(live,candidate.get('character_activity',''),now()):return False
                settings=preferences.load()
                if not settings.get('spontaneous_images',True):return False
                if live.get('busy_until') and datetime.fromisoformat(live['busy_until'])>now():return False
                today=now().date().isoformat()
                sends=[a for a in live.get('drawing_sends',[]) if a.get('day')==today and a.get('kind')!='explicit']
                if len(sends)>=settings['drawing']['daily_budget']:return False
                if sends and (now()-datetime.fromisoformat(sends[-1]['at'])).total_seconds()<settings['drawing']['share_interval_minutes']*60:return False
                kind=job.data['decision'].get('kind')
                limit=settings['drawing'].get('kind_limits',{}).get(kind)
                if limit and sum(a.get('kind')==kind for a in sends)>=limit['daily_budget']:return False
            initiative_ok=not (job and job.data.get('initiative')) or (preferences.load().get('proactive_enabled',False) and preferences.load().get('spontaneous_images',True) and (not live.get('busy_until') or datetime.fromisoformat(live['busy_until'])<=now()))
            return (initiative_ok and live['state']==scene and time.monotonic()-started<remaining_ttl
                    and (not task or not task.cancelling())
                    and (not job or live.get('scene_hair','')==job.snapshot.get('hairstyle','') or
                         live.get('scene_hair') is None and job.snapshot.get('hairstyle')=='long blue-violet hair'))
        async def batch():
            for index in range(picture_count):
                if job and index:job.data.pop('prompt_id',None)
                if not valid():
                    raise Superseded()
                async with self.image_slots.slot(job.data['decision']['priority'] if job else 0):
                    if not valid():raise Superseded()
                    if job:job.record('generating',reason='gpu_slot_acquired')
                    for attempt in range(cfg.get('generation_retries',1)+1):
                        try:
                            image = await self.comfy.image(prompt,job=job)
                            break
                        except (ImageFailure,aiohttp.ClientConnectionError,asyncio.TimeoutError) as exc:
                            if isinstance(exc,ImageFailure) and not exc.retryable:raise
                            if attempt>=cfg.get('generation_retries',1):raise
                            if not valid():raise Superseded()
                            if job:job.record('recovering',reason='transient_retry',attempt=attempt+1,**(exc.receipt() if isinstance(exc,ImageFailure) else {}))
                            await asyncio.sleep(1)
                if job:
                    (job.path/f'image_{index+1}.png').write_bytes(image)
                if not valid():
                    raise Superseded()
                digest=hashlib.sha256(image).hexdigest()
                if digest in self.store.get(key).get('sent_image_hashes',[]):
                    if job:job.record('duplicate_suppressed',image_sha256=digest)
                    return
                if job:job.record('validating',reason='pixels_downloaded')
                qa=await image_director.inspect_image(self.http,image,job.snapshot if job else {},ROOT/'data',cfg)
                if job:job.record('quality_checked',quality=qa,image_sha256=digest)
                if not qa['pass']:
                    qa_reason='quality_rejected' if qa.get('status')=='checked' else qa.get('status','quality_rejected')
                    if job:job.record('withheld_quality',reason=qa_reason)
                    if job:
                        async with self.locks.setdefault(key,asyncio.Lock()):
                            live=self.store.get(key)
                            live['shot_attempts']=(live.get('shot_attempts',[])+[shot_director.history_entry(job.snapshot,qa,job.data.get('render_config',{}),job.id)])[-4:]
                            self.store.save(key,live)
                    if job and job.data['decision'].get('kind')=='explicit':
                        notice_token=CURRENT.set(None)
                        try:
                            notice=image_director.quality_notice(qa['status'])
                            await self.send(event,notice,background=True,image_guard=valid)
                            async with self.locks.setdefault(key,asyncio.Lock()):
                                live=self.store.get(key)
                                live['history']=(live['history']+[{'role':'assistant','content':notice,'at':now().isoformat()}])[-20:]
                                self.store.save(key,live)
                            job.record('withheld_quality',reason=qa_reason,user_notified=True)
                        finally:
                            CURRENT.reset(notice_token)
                    return
                if qa.get('degraded') and job and job.data['decision'].get('kind')=='explicit':
                    await self.send(event,'这张按当前设置只完成了基础画面检查，尚未经完整语义检查。',background=True,image_guard=valid)
                    job.record('degraded_notice',quality=qa)
                # Do not cancel once handed to QQ: wait for acknowledgement and commit it.
                # New messages cancel the outer task, while this small delivery operation finishes.
                if job:job.record('ready_to_send',reason='quality_passed')
                dispatched=asyncio.Event()
                def dispatched_record():
                    dispatched.set()
                    if job:job.record('sending',reason='handed_to_qq')
                async def deliver():
                    try:
                        acknowledgement=await self.send(event,text=job.data.get('caption') or None if job else None,image=image,background=True,image_guard=valid,on_dispatch=dispatched_record)
                    except Exception as exc:
                        if job and dispatched.is_set():
                            if isinstance(exc,ImageFailure) and exc.code=='qq_send_rejected':
                                job.record('failed',reason='qq_send_rejected',**exc.receipt(),automatic_resend=False)
                            else:job.record('qq_result_unknown',reason='qq_ack_unknown',error=type(exc).__name__,automatic_resend=False)
                        raise
                    async with self.locks.setdefault(key,asyncio.Lock()):
                        session=self.store.get(key)
                        stamp=now().isoformat(timespec='seconds');session['last_image_sent_at']=stamp
                        session['image_events']=[s for s in session.get('image_events',[]) if isinstance(s,str) and s.startswith(stamp[:10])][-9:]+[stamp]
                        session['drawing_sends']=(session.get('drawing_sends',[])+[{'day':stamp[:10],'at':stamp,'kind':job.data['decision'].get('kind','explicit') if job else 'explicit'}])[-100:]
                        if job:
                            shot=job.snapshot
                            session['shot_history']=(session.get('shot_history',[])+[shot_director.history_entry(shot,qa,job.data.get('render_config',{}),job.id)])[-12:]
                            session['recent_visuals']=(session.get('recent_visuals',[])+[{'key':image_director.fingerprint([shot['location'],shot['outfit'],shot['action'],shot['camera']]),'action':shot['action'],'camera':shot['camera'],'location':shot['location']}])[-5:]
                        session['last_image_context']={'snapshot':job.snapshot if job else {},'at':stamp,'source':'fictional_illustration'}
                        session['sent_image_hashes']=(session.get('sent_image_hashes',[])+[digest])[-30:]
                        session['history']=(session['history']+[{'role':'assistant','content':'[图片已实际发送成功]'+(' '+job.data.get('caption','') if job else ''),'at':stamp}])[-20:]
                        self.store.save(key,session)
                    if job:
                        job.record('sent',reason='qq_acknowledged',qq_result=acknowledgement,sent_index=index+1)
                        self.image_decision_record(key,{'image_task_id':job.id,'event':job.data.get('related_event_id'),'kind':job.data['decision'].get('kind'),'status':'sent','reason':'qq_acknowledged'})
                        with (ROOT/'data'/'delivery_metrics.jsonl').open('a',encoding='utf-8') as archive:
                            archive.write(json.dumps({'event':'image_sent','image_task_id':job.id,'conversation':job.data.get('conversation_id'),'at':stamp,'sent':True})+'\n')
                delivery=asyncio.create_task(deliver())
                try:
                    await asyncio.shield(delivery)
                except asyncio.CancelledError:
                    if dispatched.is_set():
                        await delivery
                    else:
                        delivery.cancel()
                        await asyncio.gather(delivery,return_exceptions=True)
                    raise
        batch_task=asyncio.create_task(batch())
        chatter=asyncio.create_task(self.image_chatter(event,key,lines,activity,batch_task,picture_count,ticket=original_ticket))
        try:
            await asyncio.wait_for(batch_task,timeout=remaining_ttl)
            if job and job.data['status']=='sent':job.record('completed')
        except (asyncio.CancelledError,Superseded):
            if job and job.data['status']=='sent':
                job.record('completed',remaining_cancelled=True)
            elif job:
                expired=time.monotonic()-started>=remaining_ttl
                job.record('expired' if expired else 'cancelled',reason='task_ttl' if expired else 'cancel_scene_or_policy_changed',gpu_may_finish=True)
        except Exception as exc:
            if job and job.data['status']=='sent':
                job.record('completed',remaining_cancelled=True)
                return
            if job and job.data['status']!='qq_result_unknown':job.record('timeout' if isinstance(exc,asyncio.TimeoutError) else 'failed',reason=exc.code if isinstance(exc,ImageFailure) else 'task_timeout' if isinstance(exc,asyncio.TimeoutError) else 'qq_send_failed' if job.data['status']=='ready_to_send' else 'image_task_failed',error=type(exc).__name__,**(exc.receipt() if isinstance(exc,ImageFailure) else {}))
            log.error('后台生图失败 (%s)：%s',type(exc).__name__,str(exc)[:600])
            try:
                CURRENT.set(None)
                if job and job.data['status']=='qq_result_unknown':
                    return
                if job and job.data['decision'].get('kind')!='explicit':return
                notice='画这张等得太久，先停下了。' if isinstance(exc,asyncio.TimeoutError) else '这张没画成，稍后再试吧。'
                await self.send(event,notice,background=True,image_guard=lambda:self.store.get(key)['state']==scene and self.image_epochs.get(key,0)==(job.data.get('image_epoch',0) if job else self.image_epochs.get(key,0)))
                async with self.locks.setdefault(key,asyncio.Lock()):
                    live=self.store.get(key)
                    live['history']=(live['history']+[{'role':'assistant','content':notice,'at':now().isoformat()}])[-20:]
                    self.store.save(key,live)
                if job:job.record(job.data['status'],reason=job.data.get('reason','image_task_failed'),user_notified=True)
            except (Exception,asyncio.CancelledError):
                pass
        finally:
            chatter.cancel()
            if not batch_task.done():batch_task.cancel()
            await asyncio.gather(chatter,batch_task,return_exceptions=True)
            reply_timing.reset(timing_token)
            CURRENT.reset(token)
            if self.image_tasks.get(key) is asyncio.current_task() and key not in self.image_queues:
                self.image_tasks.pop(key,None)

    async def image_chatter(self, event, key, lines, activity, image_job, picture_count=1,ticket=None):
        config = preferences.load()
        if not config['image_chat']:
            return
        interval = max(5., float(config['image_chat_interval']))
        limit = min(len(lines), int(config['image_chat_max']))
        original_ticket=ticket or self.scheduler.current.get(key)
        for line in lines[:limit]:
            try:
                await asyncio.wait_for(asyncio.shield(image_job), timeout=interval*(.85+secrets.randbelow(31)/100))
                return
            except asyncio.TimeoutError:
                pass
            if image_job.done() or self.activity.get(key, 0) != activity:
                return
            if not self.connected.is_set():
                return
            try:
                async with self.locks.setdefault(key, asyncio.Lock()):
                    if image_job.done() or self.activity.get(key, 0) != activity:
                        return
                    token=CURRENT.set(original_ticket)
                    try:
                        await self.send(event,line,image_guard=lambda:not image_job.done())
                    finally:
                        CURRENT.reset(token)
                    session = self.store.get(key)
                    session['history'] = (session['history'] + [{'role': 'assistant', 'content': dialogue.render_time(line, now()), 'at': now().isoformat(timespec='seconds')}])[-20:]
                    self.store.save(key, session)
            except Exception:
                log.warning('追加聊天发送失败；生图继续')
                return

    async def prepare_candidate(self,event,key,candidate,settings,data=None):
        async with self.comfy.gpu.reservation():
            return await self._prepare_candidate(event,key,candidate,settings,data)

    async def _prepare_candidate(self,event,key,candidate,settings,data=None):
        self.locks.setdefault(key,asyncio.Lock())
        CURRENT.set(None)
        candidate=candidate.data()
        epoch=self.image_epochs.get(key,0)
        self.image_decision_record(key,{'image_task_id':candidate['candidate_id'],'related_event_id':candidate['dedupe_key'],'kind':candidate['source_type'],'status':'candidate','reason':candidate['trigger_reason']})
        session=self.store.get(key)
        data=dict(data or {})
        decision=image_director.decide(session,candidate['character_activity'],{**data,'_candidate':candidate},now(),settings['drawing'],settings.get('spontaneous_images',True))
        decision.setdefault('image_task_id',candidate['candidate_id'])
        self.image_decision_record(key,decision)
        if not decision['allowed']:return
        if sum(len(q) for q in self.image_queues.values())+len(self.image_tasks)>=settings['drawing'].get('queue_limit',6):
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'queue_full'});return
        session['candidate_plans']=(session.get('candidate_plans',[])+[{'day':now().date().isoformat(),'kind':candidate['source_type']}])[-100:]
        session['candidate_attempts']=(session.get('candidate_attempts',[])+[candidate['dedupe_key']])[-100:]
        self.store.save(key,session)
        # No paid request to decide whether a candidate exists. Only accepted candidates need visual planning.
        request='为这个已审核的候选准备插画，保持保存的身份、服装和地点。活动是虚拟生活演出，若日程与地点冲突就不画，不编造出行。输出完整visual_scene和visual_tags、贴合事件的action/props/camera；可给image_caption一条符合人设的短分享文字，不声称是相机实拍。'+json.dumps(candidate,ensure_ascii=False)
        if complete_from_cache(session,{**data,'state':session['state']}) or image_concept.needs_plan(data):
            request+=' 最近成功图片的动作/构图（避免连续雷同）：'+json.dumps(session.get('recent_visuals',[])[-3:],ensure_ascii=False)
            try:
                data=await deepseek(self.http,session['state'],session['history'],request,True,context=cache_context(session))
            except Exception as exc:
                self.image_decision_record(key,{**decision,'allowed':False,'status':'failed','reason':'visual_planning_failed','error':type(exc).__name__});return
        live=self.store.get(key)
        import social_reasoning
        if self.image_epochs.get(key,0)!=epoch:
            self.image_decision_record(key,{**decision,'allowed':False,'status':'cancelled','reason':'cancelled_during_candidate_planning'});return
        if social_reasoning.boundary_blocks(live,candidate['character_activity'],now()):return
        if live['state']!=candidate['scene_description'] or datetime.fromisoformat(candidate['expires_at'])<=now():
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'candidate_expired_or_scene_changed'});return
        if not settings.get('spontaneous_images',True) or not preferences.load().get('spontaneous_images',True):return
        if event.get('_initiative') and not preferences.load().get('proactive_enabled',False):return
        if live.get('busy_until') and datetime.fromisoformat(live['busy_until'])>now():return
        if data.get('state')!=candidate['scene_description']:
            self.image_decision_record(key,{**decision,'allowed':False,'reason':'visual_plan_scene_conflict'});return
        data['state']=dict(live['state'])
        data['send_image']=True
        if candidate['source_type']=='life' and not data.get('image_caption'):
            data['image_caption']='这一会儿，'+candidate['character_activity']+'。'
        data['_candidate']=candidate
        data['image_plan']={**data.get('image_plan',{}),'kind':candidate['source_type']}
        assemble(live,data)
        if candidate['source_type']=='life':
            live['life_state']={'character':character_key(live['state']['character']),'activity':candidate['character_activity'],'started_at':candidate['created_at'],'expires_at':candidate['expires_at'],'source':'fictional_routine_enactment'}
        if complete_from_cache(live,data) or image_concept.needs_plan(data):
            job=image_director.Job(ROOT/'data'/'image_jobs',{'state':copy.deepcopy(live['state'])},decision,'',self.activity.get(key,0))
            await self.prepare_image(event,key,candidate['character_activity'],data,decision,settings,False,job,epoch)
        else:self.launch_image(event,key,'',live,data,decision,settings)

    async def initiative_tick(self):
        import initiative,image_candidates
        settings=preferences.load()
        if os.getenv('IMAGE_MODE','off')!='auto' or not settings.get('proactive_enabled',False):return
        for user in sorted(self.users):
            key=f'private:0:{user}'
            if key in self.inbox_workers or key in self.image_tasks or self.image_slots.active:continue
            session=self.store.get(key);current=now()
            if not initiative.eligible(session,current,settings,self.connected.is_set()):continue
            routine_user=str(user)+':'+character_key(session['state']['character'])
            event=self.context.routine.current_event(routine_user,current)
            import social_reasoning
            candidate=image_candidates.life(key,session,current,event)
            if not candidate or social_reasoning.boundary_blocks(session,candidate.character_activity,current):continue
            session['initiative_at']=current.isoformat()
            session['initiative_attempts']=(session.get('initiative_attempts',[])+[{'day':current.date().isoformat(),'event':candidate.related_event_id}])[-20:]
            self.store.save(key,session)
            await self.prepare_candidate({'message_type':'private','user_id':user,'_initiative':True},key,candidate,settings)

    async def run(self):
        if not self.users:
            raise ValueError('请在 .env 的 ALLOWED_USERS 填写你的 QQ 号码')
        headers = {'Authorization': 'Bearer ' + os.environ['NAPCAT_TOKEN']}
        delay = 2
        while True:
            try:
                async with self.http.ws_connect(os.getenv('NAPCAT_WS_URL', 'ws://127.0.0.1:3001'),
                        headers=headers, heartbeat=30, max_msg_size=32*1024*1024) as ws:
                    self.ws = ws
                    self.connected.set()
                    delay = 2
                    log.info('已连接 NapCat，等待白名单用户消息')
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            e = json.loads(message.data)
                        except ValueError:
                            continue
                        if not isinstance(e, dict):
                            continue
                        echo = e.get('echo')
                        if isinstance(echo, str) and echo in self.pending:
                            future = self.pending[echo]
                            if not future.done():
                                future.set_result(e)
                            continue
                        accepted = self.accept(e)
                        if accepted and len(self.tasks) < 20:
                            e['_ws'] = ws
                            self.enqueue(e, *accepted)
                    log.warning('NapCat 连接已关闭，close_code=%s，%s 秒后重连', ws.close_code, delay)
            except asyncio.CancelledError:
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError('机器人正在退出'))
                tasks = list(self.tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning('NapCat 连接中断或不可用 (%s)，%s 秒后重连', type(exc).__name__, delay)
            finally:
                self.connected.clear()
                self.ws = None
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError('NapCat 连接断开'))
                # Keep in-progress rendering alive across transient QQ disconnects.
            await asyncio.sleep(delay)
            delay = min(delay*2, 30)


async def initiative_loop(instance, sleep=asyncio.sleep):
    while True:
        await sleep(60)
        try:
            await instance.initiative_tick()
        except Exception as exc:
            log.warning('自主分享候选未完成：%s',type(exc).__name__)


async def routine_loop(instance, http):
    while True:
        try:
            user=os.getenv('OWNER_QQ') or os.getenv('ALLOWED_USERS','').split(',')[0].strip()
            if user:
                current=now()
                character=instance.store.get(f'private:0:{user}')['state']['character']
                await instance.context.routine.ensure(http,user+':'+character_key(character),current,character)
        except Exception as exc:
            log.warning('日程后台准备失败：%s',type(exc).__name__)
        await asyncio.sleep(1800)


async def main(check=False):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        if check:
            await Comfy(http).check()
            print('ComfyUI 节点与模型选项检查通过。此检查不生成图片、不调用 DeepSeek。')
            return
        for key in ('DEEPSEEK_API_KEY', 'NAPCAT_TOKEN'):
            value = os.getenv(key, '')
            if not value or '填写' in value or '替换' in value:
                raise ValueError(f'请先在 .env 配置 {key}')
        now()  # Validate timezone before connecting.
        run_lock = services.take_bot_lock()
        if run_lock is None:
            log.info('助手已在运行，忽略重复启动')
            return
        try:
            instance=Bot(http)
            daily_task=asyncio.create_task(routine_loop(instance,http))
            initiative_task=asyncio.create_task(initiative_loop(instance))
            gpu_task=asyncio.create_task(instance.comfy.gpu.run())
            profile_task=None
            if preferences.load().get('notes_auto',True):
                profile_task=asyncio.create_task(instance.context.personality(http,now()))
            try:
                await instance.run()
            finally:
                daily_task.cancel()
                initiative_task.cancel()
                gpu_task.cancel()
                await asyncio.gather(daily_task,initiative_task,gpu_task,return_exceptions=True)
                if profile_task:
                    profile_task.cancel()
                    await asyncio.gather(profile_task,return_exceptions=True)
        finally:
            run_lock.close()


if __name__ == '__main__':
    (ROOT / 'data').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            ROOT / 'data' / 'bot.log', maxBytes=2*1024*1024,
                            backupCount=3, encoding='utf-8')])
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='检查 ComfyUI 的工作流兼容性')
    args = parser.parse_args()
    try:
        asyncio.run(main(args.check))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error('%s: %s', type(exc).__name__, exc)
        raise SystemExit(1)
