"""Reconcile original prompt IDs after restart. Never submit or send here."""
import asyncio
import json
import re
import time
from pathlib import Path
import aiohttp
from image_director import Job
from image_progress import ACTIVE

RECOVERABLE=ACTIVE|{'resuming','recovering','downloading','generated','reconciling'}

def load_job(path):
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('id')!=path.parent.name:raise ValueError('Invalid job')
    if not re.fullmatch(r'[a-f0-9]{32}',path.parent.name):raise ValueError('Invalid job ID')
    job=Job.__new__(Job);job.id=data['id'];job.path=path.parent;job.data=data;job.snapshot=data.get('snapshot',{})
    return job

async def reconcile(http,url,root,started_at,ttl=600):
    results=[]
    async def get(route):
        async with http.get(url.rstrip('/')+route,timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status();return await response.json()
    queue=None
    for path in sorted((Path(root)/'data'/'image_jobs').glob('*/job.json')):
        try:
            job=load_job(path);data=job.data
            if data.get('status') not in RECOVERABLE or float(data['created_at'])>=started_at:continue
            def record(status,reason,**fields):
                job.record(status,reason=reason,reconciled=True,automatic_resend=False,**fields)
                results.append({'id':job.id,'status':status,'reason':reason})
            # A persisted QQ receipt wins, including a crash between sent and completed.
            if (data.get('qq_result') or {}).get('message_id'):
                record('completed','restart_found_qq_ack');continue
            if data.get('status')=='sending':
                record('qq_result_unknown','restart_during_qq_dispatch');continue
            pid=data.get('prompt_id')
            if not pid:
                if data.get('reason')=='submission_unknown':record('outcome_unknown','submission_unknown')
                else:record('failed','restart_before_gpu_submission')
                continue
            if not isinstance(pid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',pid):
                record('outcome_unknown','invalid_prompt_id');continue
            if data.get('decision',{}).get('kind')!='explicit' and time.time()-data['created_at']>ttl:
                record('expired','restart_stale_automatic_image');continue
            try:
                history=await get('/history/'+pid)
                if not isinstance(history,dict):raise ValueError('Invalid history')
                entry=history.get(pid)
                if entry:
                    status=entry.get('status',{})
                    if status.get('status_str')=='error':
                        record('failed','reconciled_gpu_error');continue
                    images=entry.get('outputs',{}).get('9',{}).get('images',[])
                    if images:
                        output=images[0]
                        if not isinstance(output,dict) or not isinstance(output.get('filename'),str):raise ValueError('Invalid output')
                        async with http.get(url.rstrip('/')+'/view',params=output,timeout=aiohttp.ClientTimeout(total=15)) as response:
                            response.raise_for_status();content=await response.read()
                        import io
                        from PIL import Image
                        with Image.open(io.BytesIO(content)) as pixels:pixels.verify()
                        (job.path/'recovered.png').write_bytes(content)
                        record('recovered_output','original_prompt_output_saved',recovered_image='recovered.png',quality_required=True)
                        continue
                    if status.get('completed'):
                        record('failed','reconciled_output_missing');continue
                if queue is None:
                    queue=await get('/queue')
                    if not isinstance(queue,dict) or not all(isinstance(queue.get(k),list) for k in ('queue_running','queue_pending')):raise ValueError('Invalid queue')
                present=any(isinstance(row,list) and len(row)>1 and row[1]==pid for key in ('queue_running','queue_pending') for row in queue[key])
                if present and time.time()-data['created_at']<ttl:
                    record('reconciling','original_prompt_still_in_queue')
                elif present:record('expired','recovery_ttl_gpu_may_finish',gpu_may_finish=True)
                else:record('outcome_unknown','original_prompt_absent_from_history_and_queue')
            except (aiohttp.ClientError,asyncio.TimeoutError,OSError,ValueError,KeyError,TypeError) as exc:
                deadline=data.setdefault('recovery_deadline',time.time()+min(ttl,600))
                record('reconciling' if time.time()<deadline else 'outcome_unknown','recovery_interface_unavailable',recovery_error=type(exc).__name__)
        except (OSError,ValueError,KeyError,TypeError):
            results.append({'path':path.parent.name,'status':'unreadable'})
    return results
