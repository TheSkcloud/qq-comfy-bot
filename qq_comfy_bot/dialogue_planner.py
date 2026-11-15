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


def plan(text, history=(), discussion=None):
    discussion=discussion or {}
    narrative=bool(re.search(r'[“”「」]|那天|那次|当时|后来|曾经|前段时间|我写|随笔|第.{0,3}集|本作',text))
    emotion=bool(re.search(r'难过|失望|低分|不公平|担心|委屈|没法原谅|心情',text))
    question=bool(re.search(r'[？?]$|怎么办|为什么|请解释|请分析',text.strip())) and not narrative
    act='Empathize' if emotion else 'Answer' if question else 'React'
    depth='extended' if re.search(r'详细|深入|推导|分情况|比较',text) else 'normal' if len(text)>120 else 'short'
    return DialoguePlan(act,None,'reserved_respectful',depth,depth,None,bool(history),narrative)
