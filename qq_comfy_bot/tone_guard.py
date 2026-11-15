"""Narrow guard for direct contempt, not disagreement or quoted dialogue."""
import re
CONTEMPT=re.compile(r'活该|自找的|不冤|陪你念课文|提前认输的代价|聊到现在才问|不然刚才那些算什么|别指望我|你早该知道')
def issues(reply):
    spoken=re.sub(r'[“「].*?[”」]', '', reply, flags=re.S)
    return ['回复含针对用户的贬低或惩罚式表达；保留对事情的观点，去掉评判用户能力和动机的部分'] if CONTEMPT.search(spoken) else []
def fallback(reply):
    parts=re.split(r'(?<=[。！？\n])',reply)
    safe=''.join(p for p in parts if not issues(p)).strip()
    return safe or '这句话我没表达好。'
