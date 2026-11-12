"""Conservative idle model unloading through ComfyUI's official deferred /free API."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import time
import aiohttp


def settings():
    return dict(enabled=os.getenv('GPU_IDLE_UNLOAD_ENABLED','1').lower() not in ('0','false','off'),
                timeout=max(60,float(os.getenv('GPU_IDLE_TIMEOUT_SECONDS','3600'))),
                interval=max(5,float(os.getenv('GPU_IDLE_CHECK_INTERVAL_SECONDS','60'))))


class GPUManager:
    def __init__(self,http,url,root,busy=None,clock=time.time,config=None):
        self.http=http;self.url=url.rstrip('/');self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.config=config or settings();self.started=clock();self.lock=asyncio.Lock()
        self.active=0;self.reserved=0;self.internal_busy=busy or (lambda:False)
        self.path=self.root/'gpu_memory_state.json';self.unload_pending=False;self.external_busy=False
        try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={}
        if not isinstance(self.state,dict):self.state={}
        last=self.state.get('last_activity_at')
        if not isinstance(last,(int,float)) or not 0<=last<=self.started:self.state['last_activity_at']=self.started
        self.state.setdefault('pending',{})
        if not isinstance(self.state['pending'],dict):self.state['pending']={'corrupt_pending_state':{'prompt_id':None}}
        self.state['pending']={str(k):v if isinstance(v,dict) else {'prompt_id':None} for k,v in self.state['pending'].items()}
        self.unload_pending=bool(self.state.get('unload_pending'))
        # Merely inspecting a workflow must not overwrite the live bot's ledger.

    def save(self):
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(self.path)

    def record(self,event,**fields):
        with (self.root/'gpu_memory.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps({'at':self.clock(),'event':event,'last_activity_at':self.state['last_activity_at'],**fields},ensure_ascii=False)+'\n')

    def submitting(self,task_id):
        self.state['pending'][task_id]={'prompt_id':None,'at':self.clock()};self.save()

    def submitted(self,task_id,prompt_id):
        self.state['pending'][task_id]={'prompt_id':prompt_id,'at':self.clock()};self.save()

    def not_submitted(self,task_id):
        self.state['pending'].pop(task_id,None);self.save()

    def completed(self,task_id,at=None):
        self.state['pending'].pop(task_id,None)
        self.state['last_activity_at']=max(self.state['last_activity_at'],at if at is not None else self.clock())
        self.state['last_completed_at']=self.state['last_activity_at'];self.state['unloaded']=False;self.save()

    @asynccontextmanager
    async def reservation(self):
        self.reserved+=1
        try:yield
        finally:self.reserved-=1

    @asynccontextmanager
    async def execution(self):
        # Arrival is visible before waiting for an in-progress unload to finish.
        self.active+=1
        try:
            async with self.lock:
                if self.unload_pending:
                    if not await self.settle():raise RuntimeError('ComfyUI model unload is not yet confirmed; submission postponed')
                yield
        finally:self.active-=1

    async def get(self,route):
        async with self.http.get(self.url+route,timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status();return await response.json()

    async def queue(self):
        value=await self.get('/queue')
        if not isinstance(value,dict) or not all(isinstance(value.get(k),list) for k in ('queue_running','queue_pending')):
            raise ValueError('Unrecognized ComfyUI queue schema')
        return value

    @staticmethod
    def busy_queue(value):return bool(value['queue_running'] or value['queue_pending'])

    @staticmethod
    def memory(value):
        devices=value.get('devices')
        if not isinstance(devices,list) or not devices:raise ValueError('Missing GPU telemetry')
        rows=[]
        for item in devices:
            if item.get('type')!='cuda':continue
            keys=('vram_total','vram_free','torch_vram_total','torch_vram_free')
            if not all(isinstance(item.get(k),(int,float)) and item[k]>=0 for k in keys):raise ValueError('Invalid GPU telemetry')
            rows.append(dict(device=item.get('index'),device_used_bytes=item['vram_total']-item['vram_free'],
                             torch_reserved_bytes=item['torch_vram_total'],torch_unused_bytes=item['torch_vram_free']))
        if not rows:raise ValueError('No CUDA device reported')
        return rows

    def observe_history(self,history,queue):
        if not isinstance(history,dict):raise ValueError('Invalid history')
        for pid,entry in history.items():
            if not isinstance(entry,dict):continue
            status=entry.get('status',{})
            messages=status.get('messages',[])
            stamps=[m[1].get('timestamp',0)/1000 for m in messages if isinstance(m,(list,tuple)) and len(m)>1 and m[0] in ('execution_success','execution_error','execution_interrupted') and isinstance(m[1],dict)]
            end=max(stamps,default=0)
            if 0<end<=self.clock() and end>self.state['last_activity_at']:
                self.state.update(last_activity_at=end,last_completed_at=end,unloaded=False)
            prompt=entry.get('prompt',[])
            client=prompt[3].get('client_id') if isinstance(prompt,list) and len(prompt)>3 and isinstance(prompt[3],dict) else None
            for task,item in list(self.state['pending'].items()):
                if item.get('prompt_id')==pid or client==task:
                    if status.get('completed') or end:self.completed(task,end or self.clock())
        # A known prompt absent from both authoritative queues has ended/cancelled.
        # Unknown submissions need a matching history receipt and remain conservative.
        if not self.busy_queue(queue):
            for task,item in list(self.state['pending'].items()):
                if item.get('prompt_id') and item['prompt_id'] not in history:self.completed(task)
        self.save()

    async def settle(self):
        previous=None;stable=0
        baseline=self.state.get('unload_baseline',[])
        deadline=time.monotonic()+12
        for _ in range(20):
            if time.monotonic()>=deadline:return False
            queue=await self.queue()
            if self.busy_queue(queue):
                self.record('unload_waiting',reason='external_work_after_free')
                await asyncio.sleep(.5);continue
            memory=self.memory(await self.get('/system_stats'))
            old_torch=sum(r.get('torch_reserved_bytes',0) for r in baseline)
            old_used=sum(r.get('device_used_bytes',0) for r in baseline)
            current_torch=sum(r['torch_reserved_bytes'] for r in memory);current_used=sum(r['device_used_bytes'] for r in memory)
            evidence=(old_torch-current_torch>1024**2 or old_used-current_used>32*1024**2 or
                      bool(baseline) and old_used<=512*1024**2 and current_torch<=64*1024**2)
            stable=stable+1 if previous is not None and abs(previous-current_used)<16*1024**2 else 0
            previous=current_used
            if current_torch<=64*1024**2 and ((evidence and stable>=2) or stable>=6):
                # No loaded models may produce no measurable drop. The official worker
                # serializes /free with execution; record this as an accepted request,
                # never as measured memory reclamation, and allow future submissions.
                self.unload_pending=False
                self.state.update(unload_pending=False,unloaded=True,unload_confirmed=bool(evidence));self.save();return True
            await asyncio.sleep(.5)
        return False

    async def tick(self):
        if not self.config['enabled']:return {'reason':'disabled'}
        try:
            if self.lock.locked() or self.active or self.reserved or self.internal_busy():return {'reason':'internal_tasks'}
            queue=await self.queue()
            history=await self.get('/history?max_items=32')
            self.observe_history(history,queue)
            if self.busy_queue(queue):
                self.external_busy=True;self.state['last_activity_at']=self.clock();self.state['unloaded']=False;self.save()
                return {'reason':'external_queue_busy'}
            if self.external_busy:
                self.external_busy=False;self.state['last_activity_at']=self.clock();self.save()
            if self.state['pending']:return {'reason':'submission_outcome_unknown'}
            if self.state.get('unloaded') and not self.unload_pending:return {'reason':'already_unloaded'}
            if self.clock()-self.started<min(self.config['interval'],self.config['timeout']):return {'reason':'startup_grace'}
            idle=self.clock()-self.state['last_activity_at']
            if idle<self.config['timeout']:return {'reason':'not_idle_long_enough','idle_seconds':idle}
            async with self.lock:
                if self.active or self.reserved or self.internal_busy() or self.state['pending']:return {'reason':'new_internal_task'}
                before=self.memory(await self.get('/system_stats'))
                queue=await self.queue()  # Final check immediately before /free.
                if self.busy_queue(queue) or self.active or self.reserved or self.internal_busy():return {'reason':'queue_recheck_busy'}
                self.unload_pending=True;self.state['unload_pending']=True;self.save()
                self.state['unload_baseline']=before;self.save()
                async with self.http.post(self.url+'/free',json={'unload_models':True,'free_memory':True},timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status>=400:
                        self.unload_pending=False;self.state['unload_pending']=False;self.save()
                    response.raise_for_status();await response.read()
                confirmed=await self.settle()
                after=self.memory(await self.get('/system_stats'))
                result=dict(reason=('unloaded' if self.state.get('unload_confirmed') else 'unload_requested_no_measurable_change') if confirmed else 'unload_pending',idle_seconds=idle,before=before,after=after,
                    torch_reserved_released_bytes=sum(r['torch_reserved_bytes'] for r in before)-sum(r['torch_reserved_bytes'] for r in after),
                    device_usage_change_bytes=sum(r['device_used_bytes'] for r in before)-sum(r['device_used_bytes'] for r in after))
                self.record('idle_unload',**result);return result
        except (aiohttp.ClientError,asyncio.TimeoutError,OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            self.record('unload_skipped',reason='state_or_interface_unavailable',error=type(exc).__name__)
            return {'reason':'state_or_interface_unavailable','error':type(exc).__name__}

    async def run(self):
        while True:
            await asyncio.sleep(self.config['interval'])
            result=await self.tick()
            if result['reason'] not in ('not_idle_long_enough','already_unloaded'):
                self.record('idle_check',**result)
