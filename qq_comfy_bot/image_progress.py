"""Conversation-owned image receipts, never model-invented progress or implicit retries."""
from datetime import datetime
import hashlib
import json
import re
from image_policy import image_requested

from image_states import ACTIVE,TERMINAL,MESSAGES,lifecycle


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
    if generic:
        from image_requests import latest
        request=latest(session)
        # An explicit non-image activity supersedes an ambiguous progress question.
        recent_users=[m.get('content','') for m in session.get('history',[])[-4:] if m.get('role')=='user']
        if recent_users and re.search(r'饭|煮|烧水|下载|安装|作业|代码|程序',recent_users[-1]) and not image_requested(recent_users[-1]):return False
        return bool(request) or recent_visual(session.get('history',[]),current)
    last=session.get('last_image_progress_at')
    if clean in ('','嗯') and re.search(r'[？?]',text) and last:
        try:return 0<=(current-datetime.fromisoformat(last)).total_seconds()<300
        except (ValueError,TypeError):pass
    return False


def snapshot(root,session,key,current,started_at=0):
    from image_requests import latest
    request=latest(session)
    identifier=request.get('job_id') if request else session.get('last_image_job_id','')
    empty={'status':'not_started','text':'这张没有开始出图，刚才没有创建绘图任务。'}
    if request.get('cancelled_at') and not identifier:return {'status':'cancelled','text':'这张已经取消，没有继续生成。'}
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):return empty
    try:job=json.loads((root/'data'/'image_jobs'/identifier/'job.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {'status':'unknown','text':'这张的任务记录暂时读不到，进度还不能确认。'}
    if not isinstance(job,dict) or job.get('conversation_id')!=hashlib.sha256(key.encode()).hexdigest()[:12]:return empty
    try:
        created=float(job['created_at'])
        # An older successful picture cannot satisfy a newer, missed request.
        for item in reversed([] if request else session.get('history',[])):
            if item.get('role')=='user' and image_requested(item.get('content','')):
                stamp=item.get('at')
                if stamp and datetime.fromisoformat(stamp).timestamp()>created+2:return empty
                break
    except (KeyError,ValueError,TypeError):return {'status':'unknown','text':'这张的任务时间还不能确认。'}
    status=job.get('status');reason=job.get('reason','')
    if not isinstance(status,str):return {'status':'unknown','text':'这张的当前进度还不能确认。'}
    if status in ACTIVE and created<started_at and not job.get('reconciled'):
        return {'status':'interrupted','text':'助手重启后，这张的结果还没确认，先不重复提交。'}
    if reason in ('submission_unknown','qq_ack_missing') or status in ('qq_result_unknown','outcome_unknown'):
        return {'status':'outcome_unknown','text':'这张的提交或发送结果还没确认，先不重复提交，避免重复。'}
    messages=MESSAGES
    return {'status':status if status in messages else 'unknown','lifecycle':lifecycle(status),'job_id':identifier,
            'text':messages.get(status,'这张的当前进度还不能确认。')}


def guard_blocked_reply(plan,data):
    """A denied proposal cannot leave an unconditional delivery promise in text."""
    receipt=data.get('_image_receipt')
    if receipt and re.search(r'在弄|在画|正在生成|出得慢|等着|别催|快好了|排队|已经发|画好了','\n'.join(plan.get('segments',[]))):
        return {**plan,'mode':'text','should_reply':True,'segments':[receipt['text']],'segment_priority':['essential']}
    if data.get('_drawing_started'):return plan
    visual=data.get('_visual_context') or data.get('_explicit_image_request') or data.get('_candidate_scheduled') or data.get('send_image') or plan.get('intent')=='image_request'
    if not visual:return plan
    text='\n'.join(plan.get('segments',[]))
    promise=bool(re.search(r'给你一眼|给你看看|(?:画|发)(?:好|完)?(?:了|一张|张)?给你|(?:等我|马上|稍后).{0,12}(?:画|发图|给你看)|再来一次|我换一下|在弄|在画|出得慢|等着|别催|快好了|正在生成|排队',text))
    if not promise:return plan
    return {**plan,'mode':'text','should_reply':True,'segments':['这次没有开始出图。'],
            'segment_priority':['essential']}
