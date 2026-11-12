"""Received input, proposed claims and QQ-acknowledged character state are distinct."""
import copy
import re
import uuid
import response_plan

def event(session,kind,current,**fields):
    session['turn_events']=(session.get('turn_events',[])+[{'id':uuid.uuid4().hex,'kind':kind,'at':current.isoformat(),**fields}])[-80:]

def receive(session,text,current,identifier=None):
    identifier=identifier or uuid.uuid4().hex
    if any(item.get('id')==identifier for item in session.get('received_inputs',[])):return identifier
    session['history']=(session.get('history',[])+[{'id':identifier,'role':'user','content':text,'at':current.isoformat()}])[-20:]
    session['received_inputs']=(session.get('received_inputs',[])+[{'id':identifier,'at':current.isoformat()}])[-40:]
    event(session,'input_received',current,input_id=identifier)
    return identifier

def stage(session,data,plan,input_id,version,current):
    pending={'id':input_id,'reply_version':version,'status':'not_sent' if plan.get('mode')=='silent' or plan.get('should_reply') is False else 'prepared','at':current.isoformat(),
        'base_scene_version':session.get('scene_version',0),'proposed_state':copy.deepcopy(data['state']),
        'changes':copy.deepcopy(data.get('_scene_changes',[])),'life_update':copy.deepcopy(data.get('life_update',{})),
        'acknowledged_text':[],'applied_fields':[]}
    session['pending_turn']=pending
    event(session,'reply_prepared',current,input_id=input_id,scene_version=session.get('scene_version',0))
    return pending

def acknowledge(session,data,text,source,ack,current,complete=False):
    pending=session.get('pending_turn',{})
    if not pending:return
    pending['acknowledged_text'].append(text)
    delivered='\n'.join(pending['acknowledged_text'])
    changed=False
    for item in pending.get('changes',[]):
        field=item.get('field');evidence=item.get('evidence','')
        if field in pending['applied_fields'] or not isinstance(evidence,str) or not evidence:continue
        # A user direction is already grounded; a narrated transition needs its own ACK.
        if evidence not in source and evidence not in delivered:continue
        if field in ('character','clothing','location'):
            session['state'][field]=data['state'][field];changed=True
        elif field=='hairstyle':session['scene_hair']=data['_scene_hair'];changed=True
        else:continue
        pending['applied_fields'].append(field)
        session['scene_changes']=(session.get('scene_changes',[])+[{'at':current.isoformat(),'changes':[item],'source':'qq_acknowledged'}])[-20:]
    if changed:session['scene_version']=session.get('scene_version',0)+1
    life=data.get('life_update')
    if isinstance(life,dict) and not pending.get('life_applied'):
        evidence=life.get('evidence') or life.get('activity','')
        if isinstance(evidence,str) and len(evidence)>=2 and evidence in delivered:
            response_plan.update_life(session,data,current)
            if session.get('life_state'):session['life_state'].update(phase='text_acknowledged',evidence=evidence,turn_id=pending['id'])
            pending['life_applied']=True
    session['history']=(session.get('history',[])+[{'id':uuid.uuid4().hex,'role':'assistant','content':text,'at':current.isoformat()}])[-20:]
    if re.search(r'我(?:觉得|更喜欢|倾向|认为|不太认同)|我的看法|比起',delivered):
        positions=session.get('assistant_positions',[])
        positions=[item for item in positions if item.get('turn_id')!=pending['id']]
        positions.append({'quote':delivered[:500],'at':current.isoformat(),'topic':source[:100],'source':'qq_acknowledged','turn_id':pending['id']})
        session['assistant_positions']=positions[-12:]
    pending['status']='acknowledged' if complete else 'partially_acknowledged'
    event(session,'text_acknowledged',current,input_id=pending['id'],message_id=(ack or {}).get('message_id'),scene_version=session.get('scene_version',0))

def positions_context(session,text):
    terms={text[i:i+2] for i in range(len(text)-1) if all('\u4e00'<=c<='\u9fff' for c in text[i:i+2])}-{'今天','什么','怎么','这个','那个','觉得'}
    found=[item for item in session.get('assistant_positions',[]) if any(term in item.get('quote','')+item.get('topic','') for term in terms)]
    return [{'quote':item['quote'],'at':item['at']} for item in found[-3:]]
