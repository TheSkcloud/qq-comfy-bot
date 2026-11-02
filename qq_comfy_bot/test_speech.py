import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import bot
import speech


class PolicyTests(unittest.TestCase):
    def test_mode_follows_conversation_purpose(self):
        cases = [('我去写作业了，晚点聊。','exit'),('我先洗澡，回来再说','exit'),
                 ('嗯嗯，知道啦。','ack'),('谢谢啦','ack'),('今天被放鸽子了','vent'),
                 ('想骂两句，先别安慰','vent'),('这个报错怎么处理','help'),('今天买了杯茶','chat')]
        for message, mode in cases:
            with self.subTest(message=message):
                self.assertEqual(speech.plan(message,[])['mode'], mode)

    def test_questions_are_not_appended_to_each_turn(self):
        self.assertEqual(speech.plan('刚才路过一家面包店，闻着好香。',[])['questions'],0)
        history = [{'role':'assistant','content':text} for text in ['真不错。','这个我也喜欢。','下次可以试试。']]
        self.assertEqual(speech.plan('今天碰到个特别有意思的小店。',history)['questions'],1)
        history.append({'role':'assistant','content':'是哪一家？'})
        self.assertEqual(speech.plan('今天碰到个特别有意思的小店。',history)['questions'],0)
        self.assertEqual(speech.plan('别总问我，随便接话',history)['questions'],0)

    def test_colloquial_particles_are_not_all_questions(self):
        for text in ['还没收拾完呢。','我也不知道为什么。','这么说也行。']:
            self.assertEqual(speech.question_count(text),0,text)
        self.assertEqual(speech.question_count('你呢？'),1)
        self.assertTrue(speech.issues('那你喜欢哪一款？','今天买了杯茶',[]))

    def test_technical_help_and_quoted_questions_are_not_trimmed(self):
        self.assertEqual(speech.issues('报错出现在哪一步？能贴一下堆栈吗？','这个错误怎么处理',[]),[])
        self.assertEqual(speech.issues('刚才那句“你喜欢哪一款？”确实问得太多了。','别再问了',[]),[])
        self.assertEqual(speech.issues('代码里是 `a ? b : c`。','代码怎么写',[]),[])

    def test_stock_invitations_and_repeated_openings_are_detected(self):
        self.assertTrue(speech.issues('如果你需要，可以随时告诉我。','今天有点烦',[]))
        previous = '这家店的面包闻起来真的很香。'
        self.assertTrue(speech.issues(previous,'刚才买了面包',[{'role':'assistant','content':previous}]))
        self.assertEqual(speech.issues('行。','嗯嗯，知道啦。',[]),[])
        self.assertEqual(speech.issues('我想慢慢讲清楚。'*35,'展开说说你的看法',[]),[])


class Response:
    def __init__(self, content=None, fail=False):
        self.content=content
        self.fail=fail
    async def __aenter__(self):
        if self.fail:
            raise asyncio.TimeoutError()
        return self
    async def __aexit__(self,*args):
        pass
    def raise_for_status(self):
        pass
    async def json(self):
        return {'choices':[{'message':{'content':self.content}}]}


class HTTP:
    def __init__(self, reply='这只猫还真不怕人。', fail=False):
        self.reply=reply
        self.fail=fail
        self.calls=[]
    def post(self,*args,**kwargs):
        self.calls.append(kwargs)
        return Response(json.dumps({'reply':self.reply,'state':{'character':'malicious change'}}),self.fail)


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data={'reply':'那只猫什么颜色？','state':{'character':'Hana'},'send_image':False,
                   'visual_tags':'','memory_updates':[{'kind':'preference','value':'茶'}],'affect':{'joy':1}}

    async def test_only_reply_changes_and_original_is_untouched(self):
        http=HTTP()
        snapshot=copy.deepcopy(self.data)
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            result=await bot.review_speech(http,self.data,'今天碰到一只猫',[],'system','http://test.invalid')
        self.assertEqual(self.data,snapshot)
        self.assertEqual(result['_speech_review']['revision'],'applied')
        for field in ('state','send_image','visual_tags','memory_updates','affect'):
            self.assertEqual(result[field],snapshot[field])
        self.assertEqual(len(http.calls),1)
        self.assertEqual(http.calls[0]['timeout'].total,12)

    async def test_normal_replies_do_not_pay_for_another_request(self):
        http=HTTP()
        self.data['reply']='这只猫还真不怕人。'
        result=await bot.review_speech(http,self.data,'今天碰到一只猫',[],'system','http://test.invalid')
        self.assertEqual(result,self.data)
        self.assertEqual(http.calls,[])

    async def test_timeout_keeps_original_and_never_loops(self):
        http=HTTP(fail=True)
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            result=await bot.review_speech(http,self.data,'今天碰到一只猫',[],'system','http://test.invalid')
        self.assertEqual(result['reply'],self.data['reply'])
        self.assertEqual(len(http.calls),1)

    async def test_unsuccessful_rewrite_is_rejected_without_truncation(self):
        http=HTTP(reply='那你喜欢什么颜色的猫？')
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            result=await bot.review_speech(http,self.data,'今天碰到一只猫',[],'system','http://test.invalid')
        self.assertEqual(result['reply'],self.data['reply'])
        self.assertEqual(result['_speech_review']['revision'],'rejected')
        self.assertEqual(len(http.calls),1)

    async def test_malformed_provider_reply_keeps_original(self):
        class BadResponse(Response):
            async def json(self):
                return {'choices':[]}
        class BadHTTP(HTTP):
            def post(self,*args,**kwargs):
                self.calls.append(kwargs)
                return BadResponse()
        http=BadHTTP()
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            result=await bot.review_speech(http,self.data,'今天碰到一只猫',[],'system','http://test.invalid')
        self.assertEqual(result['reply'],self.data['reply'])
        self.assertEqual(len(http.calls),1)


if __name__=='__main__':
    unittest.main()
