"""Turn-aware conversational delivery and selective revision, without canned replies."""
import re


EXIT = re.compile(r'晚点聊|回头聊|先不聊|回来再(?:聊|说)|我(?:先)?(?:去|要).{0,12}(?:作业|忙|睡|上课|开会)|先走了|睡了|拜拜|先忙')
ACK = re.compile(r'^(?:(?:嗯+|哦+|噢+|好[的呀啊吧]?|行[吧啊]?|知道[啦了]?|收到|哈哈+|谢谢(?:你|啦|了)?|谢啦|[😂🤣👍👌]+))+$')
HELP = re.compile(r'怎么(?:办|做|处理|解决|写|算|用|修)|为什么|帮我|解释|分析|教程|区别|步骤|报错|代码|Traceback|Error|公式|证明|翻译', re.I)
VENT = re.compile(r'累|烦|难过|委屈|失落|不开心|放鸽子|倒霉|不想听安慰|想(?:吐槽|骂)')
NO_QUESTIONS = re.compile(r'(?:别|不要|不想|不用).{0,6}(?:问|反问|追问)|随便接话')
QUESTION = re.compile(r'[^。！!？?\n]*[？?]|[^。！!？?\n]*吗[。\s]*$|你呢[。\s]*$')
STOCK = re.compile(r'有什么(?:我)?可以帮(?:助)?你|还有什么.{0,8}(?:想聊|想了解|需要)|如果你(?:愿意|需要).{0,18}(?:随时|告诉我)|随时(?:可以)?(?:找我|告诉我)|我会一直(?:在这里|陪着你)|你的感受是(?:正常|合理|有效)的|听起来你(?:今天|现在|似乎|好像)|你想先聊哪|需要我为你')


def question_count(text):
    return len(QUESTION.findall(text))


def plan(message, history):
    recent = [m.get('content','') for m in history[-10:] if m.get('role') == 'assistant'
              and not m.get('content','').startswith('[图片已')][-3:]
    mode = 'chat'
    help_needed = bool(HELP.search(message))
    if re.search(r'修好了|解决了|搞定了|跑通了', message) and not re.search(r'怎么|为什么|帮我|解释|分析', message):
        help_needed = False
    from social_reasoning import local_emotion
    emotional=local_emotion(message,history) not in ('neutral','fatigue')
    explicit_help=bool(re.search(r'帮我|怎么办|怎么做|怎么解决|分析|解释|建议|规则|代码|公式|证明|教程|步骤|计算',message))
    if emotional and not explicit_help:
        help_needed=False
        mode='vent'
    if help_needed:
        mode = 'help'
    elif EXIT.search(message):
        mode = 'exit'
    elif ACK.fullmatch(re.sub(r'[\s，,。！!～~]+', '', message)):
        mode = 'ack'
    elif VENT.search(message):
        mode = 'vent'
    from dialogue import interaction
    interaction_plan=interaction(message,history)
    if interaction_plan['intent']:
        mode='exit' if interaction_plan['joke_end'] else 'teasing'
    recent_questions = sum(question_count(reply) > 0 for reply in recent[-2:])
    # A follow-up is earned by context, not appended to keep every turn going.
    questions = 1 if mode == 'chat' and len(recent) >= 3 and recent_questions == 0 and len(message) >= 12 else 0
    if mode == 'help':
        questions = 1  # Necessary clarification remains available.
    if NO_QUESTIONS.search(message):
        questions = 0
    return {'mode':mode, 'questions':questions, 'recent':recent}


def instruction(message, history):
    policy = plan(message, history)
    parts = ['本轮表达方式：日常默认minimal/short，一句具体反应就够，确认可几个字；不复述、不补总结，不为显得完整而解释。重要事件可normal，明确求助可extended。可以有主见、直接不赞同或不以为然，态度要有依据，不每轮唱反调。',
             '可以省主语、用短分句、自然停住；有时一句反应就够。口语靠选词和节奏，不靠密集的“呀呢嘛”、故意错字、强塞网络梗或乱加动作。',
             '不把每次分享都变成采访；不使用“你觉得呢/还有什么想聊的/需要我帮你吗”收尾。避免端着的安慰、总结和邀请。']
    modes = {
        'exit':'用户要去做自己的事。这轮简短答应即可，不附加提醒、建议、问题或自己的活动汇报。',
        'ack':'这是短短的接话或确认。回应可以同样短，不重新开题，不强行升华。',
        'vent':'用户在表达情绪。说一句贴着具体事情的反应即可；没问怎么办就别开药方、做心理解释或提醒放松休息。',
        'help':'用户在求实质帮助。先解决问题；代码、步骤和必要说明保留，不能为追求口语而省掉关键内容。只在确实缺少信息时追问。',
        'teasing':'用户可能在打趣或评价你的表达。参考前文理解指向，允许被逗笑、尴尬或轻微回击，不反复反问或维护字面正确；没有把握就自然承认。',
        'chat':'顺着眼前一个点给自己的反应、想法或适量分享，不替用户补经历。无需把这轮聊成一个完整段落。',
    }
    parts.append(modes[policy['mode']])
    parts.append('用户没明确说出的心情、想法和未来安排，不替他补。错过聚会可以回应时间撞在一起，不直接说他空落落；物品落在某处，不断言他明天要去取。可以有自己的反应，但不要把推断包装成用户的事实。')
    parts.append('事实、可能的感受和用户确认的情绪要分开。不从一个“嗯”推断心情不好，不替用户下情绪结论。学习进展或普通分享不附送巩固练习、喝水休息等建议；用户明确求建议再给。对观点保留自己的依据，调侃不等于新事实；没看懂图片就承认没有识别结果。')
    if policy['questions'] == 0:
        parts.append('这轮用陈述或感叹接住，不向用户提出新问题；用户本来在问你时直接答。引用原话里的问题不受影响。')
    else:
        parts.append('最多一个贴着当前话题的必要问题；有自然的陈述可说时就不问，不转向泛泛采访。')
    if policy['recent']:
        parts.append('参考最近自己的表达，换一个具体切入点，不复用开头和安慰句，也不故意换一种人格。')
    from dialogue import interaction_context
    parts.append(interaction_context(message,history))
    return '\n'.join(parts)


def issues(reply, message, history):
    """Only clear style violations qualify; never clip or rewrite technical material by regex."""
    policy = plan(message, history)
    if policy['mode'] == 'help' or '```' in reply or 'http://' in reply or 'https://' in reply:
        return []
    # Do not mistake quotations, commands, or example dialogue for the bot asking a question.
    spoken = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"', '', reply)
    flags = []
    if question_count(spoken) > policy['questions']:
        flags.append('不合时宜的追问或连续问句')
    if STOCK.search(spoken):
        flags.append('客服式邀请或套式心理安慰')
    clean = re.sub(r'[\W_]+', '', reply)
    for previous in policy['recent']:
        old = re.sub(r'[\W_]+', '', previous)
        if len(clean) >= 8 and (clean == old or len(old) >= 10 and clean[:10] == old[:10]):
            flags.append('重复近期回复或开头')
            break
    if len(reply) > 240 and len(message) < 70 and not re.search(r'详细|多说|展开|讲讲|说说|故事|长一点', message):
        flags.append('简短日常消息却回复一大段')
    from dialogue import interaction
    context=interaction(message,history)
    if context['meta'] and re.fullmatch(r'(?:看出来|知道|懂|明白|理解)(?:了|一点|啊|啦|的)?[。！!\s]*',spoken):
        flags.append('无具体依据声称理解言外之意')
    if context['joke_end'] and len(spoken)>100:
        flags.append('笑话已冷场仍长篇解释')
    return flags
