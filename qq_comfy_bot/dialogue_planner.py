"""Local response direction: no model calls, facts, permissions or state writes."""
import re
from dataclasses import dataclass, asdict

@dataclass(frozen=True)
class DialoguePlan:
    primary_act: str
    secondary_act: str | None
    tone: str
    response_depth: str
    response_length: str
    question_goal: str | None
    topic_continuation: bool
    quoted_or_narrative: bool

    def prompt(self):
        return ('本轮回应方向（不输出这些标签）：'+str(asdict(self))+
                '\n克制不等于训斥：不因用户分享就指责、嘲讽其能力或猜测其动机。不为显得有性格强行反驳。'
                '先接住具体内容；意见不同可以说理由，不用“你早该知道”“不冤”“陪你念课文”评判用户。'
                '叙述中的引语、反问和文章里的“你”未必是在对你提问或辱骂；先识别说话人与指向对象。'
                '回忆按叙事时间理解，缺少日期就不计算“拖了多久”。'
                '夸张、段子可以轻松接住，不把夸张因果当真，也不逐句训话。'
                '评价不同维度不等于矛盾，例如主题表达成立而整体体验不佳可以并存。'
                '不添加用户未说过的人格、经历或因果。没有必要就不追问，也不用每轮造笑点。')


def plan(text, history=(), discussion=None, *, relation=None, tone='reserved_respectful', confirmed_boundary=False):
    import speech
    from memory_recall import terms
    discussion = discussion or {}
    spoken = re.sub(r'[“「].*?[”」]', '', text, flags=re.S).strip()
    narrative = spoken != text.strip() or bool(re.search(r'那天|那次|当时|后来|我写|本作|我问他|我问她|他跟我说|她跟我说', text))
    policy = speech.plan(spoken, history)
    mode = policy['mode']
    goal = None
    if confirmed_boundary and re.search(r'不想聊|到此为止|别追问', spoken):
        act = 'Close'
    elif mode == 'exit':
        act = 'Close'
    elif mode == 'ack':
        act = 'Acknowledge' if not history else 'AllowSilence'
    elif re.search(r'你怎么看|你觉得呢|你同意吗|你的看法', spoken):
        act = 'TakePosition'
    elif mode == 'help' or re.search(r'(?:请解释|详细讲|为什么)', spoken):
        act = 'Explain'
    elif spoken.endswith(('？','?')) or re.search(r'听说过|知道.{1,18}吗', spoken):
        act = 'Answer'
    elif mode == 'vent':
        act = 'Listen'
    elif policy['questions'] and re.search(r'没听懂|有个问题|卡住|不知道选哪',spoken):
        act, goal = 'Clarify', '问清卡住的环节'
    elif mode == 'teasing' or re.search(r'笑死|糗事|社死|离谱',spoken):
        act = 'PlayAlong'
    elif narrative:
        act = 'ListenToStory'
    else:
        act = 'React'
    previous = next((m.get('content','') for m in reversed(history) if m.get('role')=='user'), '')
    follow = bool(previous and (terms(previous) & terms(spoken) or re.match(r'所以|但是|接着|还有|那我|你刚才',spoken)))
    if relation in ('switch','side') or re.search(r'换个话题|说点别的',spoken):
        follow = False
    elif relation == 'resume':
        follow = True
    length = 'extended' if re.search(r'详细|深入|推导|分情况|比较', spoken) else 'minimal' if act in ('Close','Acknowledge','AllowSilence','PlayAlong') else 'short'
    secondary = 'Example' if act=='Explain' and re.search(r'例子|举例',spoken) else None
    return DialoguePlan(act, secondary, tone, length, length, goal, follow, narrative)
