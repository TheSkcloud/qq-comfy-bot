"""One bounded visual-only recovery for explicit requests, never fabricated scene tags."""
import json
import os
import aiohttp
from visual_brief import validate_brief
import image_concept


async def complete_visual(http,url,state,user,context=''):
    body={'model':os.getenv('DEEPSEEK_MODEL','deepseek-flash'),
          'messages':[{'role':'system','content':
              '为已接受的绘图任务规划创意及视觉方案，只输出JSON：creative_brief、visual_scene:{identity,outfit,setting,action,composition}。'+image_concept.instruction()+
              '五字段都必须为英文视觉标签，保持给定身份、服装、地点、发型，仅按用户明确要求改变。'
              '只描述单幅动漫插画；不输出对白，不编造用户的动作。'},
              {'role':'user','content':json.dumps({'state':state,'request':user,'context':context[-4000:]},ensure_ascii=False)}],
          'response_format':{'type':'json_object'},'thinking':{'type':'enabled'},'reasoning_effort':'medium','max_tokens':6000}
    async with http.post(url,json=body,headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']},timeout=aiohttp.ClientTimeout(total=90)) as response:
        response.raise_for_status();result=await response.json()
    choice=result['choices'][0]
    if choice.get('finish_reason')=='length':raise ValueError('Visual recovery truncated')
    from response_plan import decode
    data=decode(choice['message']['content'],True)
    # Some compatible providers flatten the five fields despite the nested schema.
    brief=validate_brief(data.get('visual_scene',data))
    if not all(brief.values()):raise ValueError('Visual recovery missing fields')
    raw=data.get('creative_brief')
    concept=image_concept.normalize(raw,brief,data.get('image_plan'))
    return {'visual_scene':brief,'visual_tags':', '.join(brief.values()),'send_image':True,
            'creative_brief':concept,'creative_brief_raw':raw,'_thinking_enabled':True,
            '_thinking_evidence':{'requested':True,'provider_reasoning_present':bool(choice['message'].get('reasoning_content')),
                'reasoning_tokens':result.get('usage',{}).get('completion_tokens_details',{}).get('reasoning_tokens')}}
