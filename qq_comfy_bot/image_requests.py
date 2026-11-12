"""Conversation-owned request ledger. Classification never calls a model."""
from datetime import datetime
import re
import uuid
from image_policy import image_requested

NEGATIVE=re.compile(r'(?:不要|别|不用|不想|不需要|先不).{0,8}(?:画|拍|自拍|照片|图片|发图)')
CONTINUE=re.compile(r'^(?:去画吧|去画|开始画吧?|画吧|继续画吧?|再画画|重来|再来一次)[。！!～~\s]*$')

def latest(session):
    items=session.get('image_requests',[])
    return items[-1] if isinstance(items,list) and items and isinstance(items[-1],dict) else {}

def classify(text,session,current):
    text=text.strip()
    result={'action':'none','text':text,'evidence':text}
    previous=latest(session)
    if NEGATIVE.search(text) or re.search(r'(?:取消|停止).{0,6}(?:画|图|拍|生成)',text) or text in ('/取消','取消'):
        return {**result,'action':'cancel'}
    if CONTINUE.fullmatch(text):
        source=previous.get('request') or session.get('last_image_request','')
        if not source:
            source=next((m.get('content','') for m in reversed(session.get('history',[]))
                         if m.get('role')=='user' and image_requested(m.get('content',''))),'')
        stamp=previous.get('at') or session.get('last_image_sent_at')
        try:
            fresh=not stamp or 0<=(current-datetime.fromisoformat(stamp)).total_seconds()<86400
        except (ValueError,TypeError):fresh=False
        if source and fresh:
            action='retry' if re.search(r'再|重来',text) else 'confirm'
            return {**result,'action':action,'text':source+'；继续指令：'+text}
        return {**result,'action':'unbound_confirm'}
    if image_requested(text,session.get('history',[])):
        return {**result,'action':'request'}
    if previous and re.search(r'(?:改成|换成|加上|去掉|改为).{0,18}(?:背景|衣|裙|发|海边|室内|全身|半身|特写|比耶|叉腰)',text):
        return {**result,'action':'amend','text':previous['request']+'；本次修改：'+text}
    return result

def model_request(text,data):
    """A model hint alone cannot authorize an image or override a denial."""
    value=data.get('image_request',{})
    if not isinstance(value,dict) or value.get('action')!='request' or NEGATIVE.search(text):return False
    evidence=value.get('evidence')
    return (isinstance(evidence,str) and len(evidence)>=4 and evidence in text
            and bool(re.search(r'给我|让我|能不能|可以|发|来一|画|拍|欣赏',evidence))
            and bool(re.search(r'自拍|近照|照片|图片|插画|肖像|一张图|一幅画',evidence))
            and not re.search(r'我在|我正在|代码|算法|函数|不要|不用|不想',evidence))

def receive(session,request,current,job_id):
    item={'id':uuid.uuid4().hex,'at':current.isoformat(),'action':request['action'],
          'request':request['text'][:4000],'evidence':request['evidence'][:2000],'job_id':job_id}
    session['image_requests']=(session.get('image_requests',[])+[item])[-30:]
    session['last_image_job_id']=job_id
    session['last_image_request']=item['request']
    return item
