import json,os,unittest
from unittest.mock import patch
import bot,dialogue,response_plan
from test_remediation import Provider

class ChatFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_plain_text_failure_recovers_in_one_call(self):
        provider=Provider('[11-01 12时] 那当我多嘴。行，吃饱了下午坐着就没那么难熬。')
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test','CHAT_SCHEMA_RETRY':'0','SPEECH_REVIEW_ENABLED':'0'}):
            data=await bot.deepseek(provider,bot.DEFAULT,[],'都这个点了，我肯定吃过了😏')
        self.assertEqual(len(provider.requests),1)
        self.assertFalse(data['send_image'])
        self.assertEqual(data['state'],bot.DEFAULT)
        self.assertFalse(data['reply'].startswith('['))
        self.assertEqual(data['memory_updates'],[])
        self.assertEqual(data['_model_format'],'plain_text_recovered')

    def test_malformed_plans_are_not_forwarded_as_text(self):
        for value in ('{','[1,2','```json\n{"reply":"x"','reply: x, state: {}',''):
            with self.subTest(value=value),self.assertRaises(ValueError):response_plan.decode(value)

    def test_plain_text_does_not_fake_photo_metadata(self):
        with self.assertRaises(ValueError):response_plan.decode('给你拍好了。',True)

    def test_history_matches_json_response_without_time_echo(self):
        history=[{'role':'user','content':'吃饭没','at':'2026-11-01T12:00:00+08:00'},
                 {'role':'assistant','content':'刚吃完。','at':'2026-11-01T12:00:05+08:00'}]
        result=dialogue.api_history(history,assistant_json=True)
        self.assertTrue(result[0]['content'].startswith('[11-01 12时]'))
        self.assertEqual(json.loads(result[1]['content']),{'reply':'刚吃完。'})
        self.assertEqual(history[1]['content'],'刚吃完。')
