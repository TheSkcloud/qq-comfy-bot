"""Meaningful short-message plans; never clip text or split code/URLs by character count."""
import re
ATTITUDES={'neutral','agreement','disagreement','teasing','sarcasm','dismissive','indifferent','curious','enthusiastic','annoyed','reluctant','sincere'}
LENGTHS={'minimal','short','normal','extended'}


def split_prose(text):
    if len(text)<80 or '```' in text or re.search(r'https?://|^\s*(?:[-*]|\d+[.)、])\s',text,re.M):return [text]
    chunks=[];start=0;quotes=[];pairs={'“':'”','「':'」','『':'』','（':'）','(':')'}
    for i,char in enumerate(text):
        if char in pairs:quotes.append(pairs[char])
        elif quotes and char==quotes[-1]:quotes.pop()
        if not quotes and char in '。！？!?\n' and text[start:i+1].strip():
            chunks.append(text[start:i+1].strip());start=i+1
    if text[start:].strip():chunks.append(text[start:].strip())
    if len(chunks)<2:return [text]
    # Group very short adjacent sentences. Each original character remains in a complete clause.
    grouped=[]
    for chunk in chunks:
        if grouped and len(grouped[-1])<12:grouped[-1]+=chunk
        else:grouped.append(chunk)
    while len(grouped)>4:
        index=min(range(len(grouped)-1),key=lambda i:len(grouped[i])+len(grouped[i+1]))
        grouped[index:index+2]=[grouped[index]+grouped[index+1]]
    return grouped


def decorate(plan,value=None):
    value=value or {}
    length=value.get('length','short')
    attitude=value.get('attitude','neutral')
    plan.update(length=length if length in LENGTHS else 'short',attitude=attitude if attitude in ATTITUDES else 'neutral',
                attitude_reason=str(value.get('attitude_reason',''))[:180])
    if plan['mode']=='text' and len(plan['segments'])==1:
        plan['segments']=split_prose(plan['segments'][0])
    priorities=value.get('segment_priority',[])
    plan['segment_priority']=[priorities[i] if isinstance(priorities,list) and i<len(priorities) and priorities[i] in ('essential','optional') else ('essential' if plan['length']=='extended' else 'optional') for i in range(len(plan['segments']))]
    return plan


def empty_important_reaction(data,user,history):
    from social_reasoning import local_emotion
    from speech import ACK
    reply=re.sub(r'[\s，。！!～~]+','',data.get('reply',''))
    important=local_emotion(user,history) not in ('neutral','fatigue') or data.get('emotion_state',{}).get('category') in {'excitement','pride','sadness','disappointment'}
    return important and bool(reply) and bool(ACK.fullmatch(reply))
