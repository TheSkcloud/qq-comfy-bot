"""Acknowledged logical turns and bounded discussion context; no extra model calls."""
import copy
import json
import re
import uuid
from datetime import datetime

def domain(text):
    patterns={'visual':r'自拍|照片|插画|画一张|画张|发图|出图',
              'career':r'职业|工作选择|找工作|就业|记者站|招聘|实习',
              'study':r'上课|课程|物理|英语|卷子|作业|图书馆|考试',
              'social':r'组长|偶遇|室友|同学聚会',
              'coding':r'代码|算法|报错|编程|程序', 'music':r'音乐|节奏|歌曲|唱歌'}
    return {key for key,pattern in patterns.items() if re.search(pattern,text)}

ACK = re.compile(r'^(?:嗯+|哦+|噢+|好[的吧啊]?|行[吧啊]?|知道了|收到|明白了|懂了|原来如此|谢谢|哈哈+|呵呵+|😂|🤣|👍)+$')
SWITCH = re.compile(r'换个?话题|另一个问题|不聊这个|先不说这个|说点别的|现在聊|改聊')
SIDE = re.compile(r'顺便问|插一句|岔开一下|先插个|题外话')
RESUME = re.compile(r'回到刚才|接着刚才|继续刚才|回到之前|接着说|刚才那个问题')


def substantive(text):
    clean=re.sub(r'[\W_]+','',text)
    if ACK.fullmatch(clean) or re.fullmatch(r'早上好|晚上好|你好|晚安|拜拜|在吗',clean):return False
    if re.fullmatch(r'凭什么|为什么|怎么会|那怎么办|然后呢|那又怎样|那第二种呢',clean):return True
    followup=len(clean)>=3 and bool(re.search(r'^(?:那|但|所以|如果|然后|第二|另一).*(?:呢|吗|为什么)$|^为什么',clean))
    return len(clean)>=6 or followup


def complex_question(text,history=()):
    if history and re.fullmatch(r'[\s？?！!]*(?:凭什么|为什么|怎么会|那怎么办)[\s？?！!]*',text):return True
    if re.search(r'认真(?:分析|想)|深入(?:分析|思考)|多角度|权衡|多条件|分情况|推导|证明|代码|调试|报错|Traceback|程序设计|算法|因果|根本原因',text,re.I):return True
    if re.search(r'为什么|原因|怎么解释|比较|对比|哪个方案|如何选择|怎么办|怎么做|分析|解释|区别',text):return True
    if len(text)>70 and (len(re.findall(r'如果|但是|然而|同时|因为|所以|假设|条件|一方面|另一方面',text))>=2 or len(re.findall(r'[？?]',text))>=2):return True
    if len(text)>10 and re.search(r'结合.{0,10}(?:前面|之前|刚才)|前面.{0,15}(?:矛盾|联系|推断)|这两件事.{0,8}(?:联系|说明|冲突)',text):return True
    return False


def empty(current,summary=''):
    return dict(topic_id=uuid.uuid4().hex,topic_summary=summary[:240],substantive_turns=0,thinking_active=False,
                important_context=[],resolved_questions=[],open_questions=[],updated_at=current.isoformat(),completed_turn_ids=[])


def plan(session,user,history,current,threshold=4):
    threshold=max(1,min(20,int(threshold)))
    old=session.get('dialogue_state')
    state=copy.deepcopy(old) if isinstance(old,dict) and old.get('topic_id') else empty(current)
    state.setdefault('suspended_topics',[])
    relation='continue'
    transition_reason='same_discussion'
    try:gap=(current-datetime.fromisoformat(state['updated_at'])).total_seconds()
    except (KeyError,TypeError,ValueError):gap=0
    old_domains=domain(state.get('last_user_text') or state.get('topic_summary',''))
    new_domains=domain(user)
    natural_switch=bool(new_domains and old_domains and not new_domains.intersection(old_domains))
    if RESUME.search(user) and state['suspended_topics']:
        suspended=state['suspended_topics'][-1]
        state={**copy.deepcopy(suspended),'suspended_topics':state['suspended_topics'][:-1]}
        relation='resume'
    elif SWITCH.search(user) or substantive(user) and (gap>4*3600 or natural_switch or state.get('missing_update_turns',0)>=12):
        suspended={k:v for k,v in state.items() if k!='suspended_topics'}
        state={**empty(current,user),'suspended_topics':(state['suspended_topics']+[suspended])[-2:]}
        relation='switch'
        transition_reason='explicit_switch' if SWITCH.search(user) else 'time_gap' if gap>4*3600 else 'subject_changed' if natural_switch else 'metadata_missing_limit'
    elif SIDE.search(user):relation='side'
    meaningful=substantive(user)
    complex_now=meaningful and complex_question(user,history)
    if meaningful and relation not in ('side','switch') and state.get('last_stage')=='C' and re.search(r'^(?:那|但|所以|如果|再|第二|另一)|前面|刚才|继续',user):complex_now=True
    reference=state.get('last_user_text') or state.get('topic_summary','')
    terms=lambda value:{value[i:i+2] for i in range(len(value)-1) if all('\u4e00'<=c<='\u9fff' for c in value[i:i+2])}-{'今天','这个','那个','现在','可以','觉得','什么','怎么','一下','我的','你的'}
    overlap=bool(terms(user)&terms(reference))
    continuation=bool(re.search(r'^(?:那|但|所以|如果|不过|也就是说)|前面|刚才|继续|接着|这个问题|这一步',user))
    unresolved=bool(state.get('open_questions')) and (continuation or overlap)
    if meaningful and re.fullmatch(r'凭什么[？?！!\s]*',user) and (history or state.get('open_questions')):complex_now=True
    deep=meaningful and relation not in ('side','switch') and int(state.get('substantive_turns',0))>=threshold and (continuation or overlap or unresolved)
    thinking=bool(complex_now or deep)
    return dict(thinking=thinking,stage='C' if complex_now else 'B' if deep else 'A',
                reason='complex_question' if complex_now else 'sustained_discussion' if deep else 'short_ack' if not meaningful else 'casual_or_early_discussion',
                relation=relation,transition_reason=transition_reason,route_confidence='supported' if continuation or overlap or complex_now else 'uncertain',substantive=meaningful,state=state,threshold=threshold,turn_id=uuid.uuid4().hex)


def should_think(decision):return decision['thinking']


def context(decision):
    state=decision['state']
    return ('讨论状态只用于承接对话，不是新的用户事实；Thinking不要求回复变长。'+json.dumps({
        'topic_id':state['topic_id'],'topic_summary':state.get('topic_summary',''),'completed_substantive_turns':state.get('substantive_turns',0),
        'important_context':state.get('important_context',[])[-12:],'resolved_questions':state.get('resolved_questions',[])[-6:],
        'open_questions':state.get('open_questions',[])[-6:],'current_stage':decision['stage'],'transition_reason':decision['transition_reason']},ensure_ascii=False)+
        '\n可在同一次输出提供dialogue_update:{relation:continue/switch/side/resume,topic_summary:简短讨论摘要,evidence:本轮用户原话连续片段,'
        'important_context:[{speaker:user/assistant,text:观点短句,evidence:对应原话连续片段}],resolved_questions:[{question,answer,evidence:reply中的连续片段}],'
        'open_questions:[{question,evidence:用户原话或reply中的连续片段}]}。每个有实质内容的回复都判断话题关系，沿用也写continue及证据；摘要是讨论记录，不能把推测包装成用户事实。')


def finish(session,user,spoken,data,decision,current):
    """Called once, only after every planned semantic segment was acknowledged."""
    if not spoken or not decision['substantive']:return
    if decision['turn_id'] in session.get('dialogue_state',{}).get('completed_turn_ids',[]):return
    state=copy.deepcopy(decision['state'])
    if decision['turn_id'] in state.get('completed_turn_ids',[]):return
    update=data.get('dialogue_update',{})
    update=update if isinstance(update,dict) else {}
    relation=decision['relation']
    evidence=update.get('evidence','')
    # Semantic topic updates from the existing generation complement local switch cues.
    if isinstance(evidence,str) and len(evidence)>=2 and evidence in user:
        proposed=update.get('relation')
        if proposed=='side':return
        if proposed=='switch' and relation not in ('switch','resume'):
            suspended={k:v for k,v in state.items() if k!='suspended_topics'}
            state={**empty(current),'suspended_topics':(state.get('suspended_topics',[])+[suspended])[-2:]}
        if isinstance(update.get('topic_summary'),str) and update['topic_summary'].strip():
            state['topic_summary']=update['topic_summary'][:300]
    if relation=='side':return
    if not state.get('topic_summary'):state['topic_summary']=user[:240]
    points=state.get('important_context',[])
    # Verbatim bounded fallback survives history trimming without inventing facts.
    points.append({'speaker':'user','text':user[:300],'evidence':user[:300],'source':'user_quote'})
    for item in (update.get('important_context',[]) if isinstance(update.get('important_context'),list) else [])[:5]:
        if not isinstance(item,dict):continue
        speaker=item.get('speaker');quote=item.get('evidence','')
        source=user if speaker=='user' else spoken if speaker=='assistant' else ''
        if isinstance(quote,str) and len(quote)>=2 and quote in source and isinstance(item.get('text'),str):
            points.append({'speaker':speaker,'text':item['text'][:300],'evidence':quote[:300],'source':'conversation_summary'})
    unique=[];seen=set()
    for point in points:
        key=(point.get('speaker'),point.get('evidence'))
        if key not in seen:unique.append(point);seen.add(key)
    state['important_context']=unique if len(unique)<=12 else unique[:2]+unique[-10:]
    for key in ('resolved_questions','open_questions'):
        items=state.get(key,[])
        for item in (update.get(key,[]) if isinstance(update.get(key),list) else [])[:3]:
            if not isinstance(item,dict):continue
            quote=item.get('evidence','')
            source=spoken if key=='resolved_questions' else user+'\n'+spoken
            if isinstance(quote,str) and len(quote)>=2 and quote in source and isinstance(item.get('question'),str):
                items.append({k:str(item[k])[:300] for k in ('question','answer','evidence') if k in item})
        state[key]=items[-6:]
    solved={re.sub(r'[\W_]+','',item['question']) for item in state['resolved_questions']}
    state['open_questions']=[item for item in state['open_questions'] if re.sub(r'[\W_]+','',item['question']) not in solved]
    state['substantive_turns']=int(state.get('substantive_turns',0))+1
    state['thinking_active']=state['substantive_turns']>=decision['threshold']
    state['last_stage']=decision['stage'];state['last_thinking']=decision['thinking']
    state['completed_turn_ids']=(state.get('completed_turn_ids',[])+[decision['turn_id']])[-20:]
    state['updated_at']=current.isoformat()
    state['last_user_text']=user[:240]
    grounded=isinstance(evidence,str) and len(evidence)>=2 and evidence in user
    state['dialogue_update_status']='accepted' if grounded else 'missing' if not update else 'invalid_evidence'
    state['missing_update_turns']=0 if grounded else int(state.get('missing_update_turns',0))+1
    state['transition_reason']=decision.get('transition_reason','same_discussion')
    session['dialogue_state']=state
