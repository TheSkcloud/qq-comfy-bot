"""Conversation-owned image receipts, never model-invented progress or implicit retries."""
from datetime import datetime
import hashlib
import json
import re
from image_policy import image_requested

ACTIVE={'received','planning','accepted','queued','submitted','generating','quality_checked','validating','ready_to_send','sending'}
TERMINAL={'completed','sent','failed','timeout','expired','cancelled','rejected','withheld_quality','duplicate_suppressed','qq_result_unknown'}


def recent_visual(history,current):
    for item in reversed((history or [])[-6:]):
        if not isinstance(item,dict):continue
        stamp=item.get('at')
        if stamp:
            try:
                if (current-datetime.fromisoformat(stamp)).total_seconds()>900:return False
            except (ValueError,TypeError):return False
        text=item.get('content','')
        if not isinstance(text,str):continue
        if re.search(r'换个?话题|先不聊|不说这个',text):return False
        if image_requested(text) or re.search(r'(?:图片|绘图|画面|出图|画好了|给你一眼|图还没|图没有|没有开始出图)',text):return True
        if item.get('role')=='user' and len(text)>8:return False
    return False


def is_query(text,session,current):
    clean=re.sub(r'[\W_]+','',text)
    if re.fullmatch(r'(?:图片|这张图|那张图|图|绘图|画)(?:好了吗|好了没|好没|呢|进度|的进度|画好了吗|画好了没|怎么样了|发了吗|怎么还没好|怎么还没发)',clean):return True
    generic=bool(re.fullmatch(r'好了没|好了吗|好没|快好了没|快好了吗|还没好|还没好吗|怎么样了|进度呢|弄好了吗|好了没有|还要多久',clean))
    if generic:return recent_visual(session.get('history',[]),current)
    last=session.get('last_image_progress_at')
    if clean in ('','嗯') and re.search(r'[？?]',text) and last:
        try:return 0<=(current-datetime.fromisoformat(last)).total_seconds()<300
        except (ValueError,TypeError):pass
    return False


def snapshot(root,session,key,current,started_at=0):
    identifier=session.get('last_image_job_id','')
    empty={'status':'not_started','text':'这张没有开始出图，刚才没有创建绘图任务。'}
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):return empty
    try:job=json.loads((root/'data'/'image_jobs'/identifier/'job.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {'status':'unknown','text':'这张的任务记录暂时读不到，进度还不能确认。'}
    if not isinstance(job,dict) or job.get('conversation_id')!=hashlib.sha256(key.encode()).hexdigest()[:12]:return empty
    try:
        created=float(job['created_at'])
        # An older successful picture cannot satisfy a newer, missed request.
        for item in reversed(session.get('history',[])):
            if item.get('role')=='user' and image_requested(item.get('content','')):
                stamp=item.get('at')
                if stamp and datetime.fromisoformat(stamp).timestamp()>created+2:return empty
                break
    except (KeyError,ValueError,TypeError):return {'status':'unknown','text':'这张的任务时间还不能确认。'}
    status=job.get('status');reason=job.get('reason','')
    if not isinstance(status,str):return {'status':'unknown','text':'这张的当前进度还不能确认。'}
    if status in ACTIVE and created<started_at:
        return {'status':'interrupted','text':'助手重启后，这张的结果还没确认，先不重复提交。'}
    if reason in ('submission_unknown','qq_ack_missing') or status=='qq_result_unknown':
        return {'status':'outcome_unknown','text':'这张的提交或发送结果还没确认，先不重复提交，避免重复。'}
    messages={
        'received':'还在准备画面，没开始出图。','planning':'还在准备画面，没开始出图。',
        'accepted':'已经接下这张，正在排队。','queued':'已经排队，还没开始画。',
        'submitted':'正在画，还没完成。','generating':'正在画，还没完成。',
        'quality_checked':'画好了，正在完成图片检查。','validating':'画好了，正在检查。',
        'ready_to_send':'图片已准备好，正在发送。','sending':'图片正在发送，还没确认送达。',
        'completed':'这张已经发出了。','sent':'这张已经发出了。',
        'withheld_quality':'这张没有通过图片检查，已经停下，没有发送。',
        'cancelled':'这张已经取消，没有继续生成。','expired':'这张等得太久，任务已经停止。',
        'failed':'这张任务失败了，已经停下。','timeout':'这张超时了，已经停下。',
        'rejected':'这张没有进入绘图队列。','duplicate_suppressed':'这张被判定重复，未再次发送。'}
    return {'status':status if status in messages else 'unknown','job_id':identifier,
            'text':messages.get(status,'这张的当前进度还不能确认。')}


def guard_blocked_reply(plan,data):
    """A denied proposal cannot leave an unconditional delivery promise in text."""
    if data.get('_drawing_started') or data.get('_image_failure'):return plan
    if not data.get('send_image') and plan.get('intent')!='image_request':return plan
    text='\n'.join(plan.get('segments',[]))
    promise=bool(re.search(r'给你一眼|给你看看|(?:画|发)(?:好|完)?(?:了|一张|张)?给你|(?:等我|马上|稍后).{0,12}(?:画|发图|给你看)',text))
    if not promise:return plan
    return {**plan,'mode':'text','should_reply':True,'segments':['这次没有开始出图。'],
            'segment_priority':['essential']}
