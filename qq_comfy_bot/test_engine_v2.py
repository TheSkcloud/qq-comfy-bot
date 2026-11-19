import asyncio,copy,json,os,tempfile,unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch,AsyncMock,Mock
import capability_registry as registry,chat_analysis,dialogue_planner,response_plan,social_reasoning,preferences,image_mode,bot
from life import Life

class ObservedLanguageTests(unittest.TestCase):
    def test_stop_commands_do_not_consume_rhetorical_statements(self):
        for text in ('我不想把话说得太死，但确实如此。','别的都不说，就说第一条。','我不愿轻易说这个结论。','老师说：“不要再聊这个了。”','他说不聊了，我也没办法。'):
            with self.subTest(text=text):self.assertFalse(social_reasoning.boundary_update({},text,datetime.now()))
        for text in ('我不想聊这个了','别再提这件事','不要跟我说这个','先不讨论这个','不聊了'):
            with self.subTest(text=text):self.assertTrue(social_reasoning.boundary_update({},text,datetime.now()))

    def test_third_person_and_quoted_facts_are_not_user_facts(self):
        for text,value in (('刘同学最近在实验室很忙','最近在实验室很忙'),('我朋友最近在实验室很忙','最近在实验室很忙'),('我听说刘同学最近在实验室很忙','最近在实验室很忙'),('我在上课，他在实验室很忙','在实验室很忙'),('老师说：“我喜欢喝茶”','喜欢喝茶')):
            evidence='我喜欢喝茶' if '老师说' in text else text
            with self.subTest(text=text),tempfile.TemporaryDirectory() as root:
                self.assertEqual(Life(root).record('u',[dict(kind='activity',value=value,evidence=evidence)],text,datetime.now()),[])

    def test_owned_parallel_preferences_survive(self):
        text='我不喜欢咖啡，喜欢热茶'
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(len(Life(root).record('u',[dict(kind='preference',value='喜欢热茶',evidence=text)],text,datetime.now())),1)

    def test_control_character_repair_cannot_authorize_side_effects(self):
        raw='{"reply":"第一句\n第二句","send_image":true,"memory_updates":[{"value":"火星"}],"affect":{"joy":2},"state":{"location":"火星"}}'
        recovered=response_plan.decode(raw)
        data=response_plan.normalize(recovered,copy.deepcopy(bot.DEFAULT),'我喜欢清茶')
        self.assertTrue(data['_text_only_recovery']);self.assertFalse(data['send_image']);self.assertEqual(data['memory_updates'],[])
        self.assertEqual(data['state'],bot.DEFAULT)
        self.assertNotIn('affect',recovered)
        with self.assertRaises(json.JSONDecodeError):response_plan.decode(raw,force_image=True)

    def test_short_referential_reasoning_uses_history(self):
        self.assertEqual(chat_analysis.route('所以这又是什么意思？',history=[{'role':'user','content':'前一个具体问题'}]),(True,'contextual_reasoning'))
        self.assertEqual(chat_analysis.route('好的')[0],False)

    def test_planner_does_not_mistake_reported_question_for_direct_question(self):
        p=dialogue_planner.plan('老师说：“你听说过这本书吗？”')
        self.assertEqual(p.primary_act,'ListenToStory');self.assertTrue(p.quoted_or_narrative)
        self.assertEqual(dialogue_planner.plan('为什么会这样？').primary_act,'Explain')
        self.assertEqual(dialogue_planner.plan('你听说过这本书吗？').primary_act,'Answer')

class CapabilityTests(unittest.TestCase):
    def test_mode_origin_matrix(self):
        for mode,explicit,automatic in [('OFF',False,False),('MANUAL',True,False),('ASSISTED',True,False),('AUTO',True,True)]:
            c=registry.resolve({'capabilities':{'image':mode}},legacy_mode='off')
            self.assertEqual(c.permits('user_explicit'),explicit);self.assertEqual(c.permits('character_auto'),automatic)
        self.assertTrue(registry.resolve({'capabilities':{'image':'ASSISTED'}}).permits('character_auto',confirmed=True))

    def test_legacy_off_preserves_manual_requests(self):
        c=registry.resolve({},legacy_mode='off')
        self.assertEqual(c.effective_mode,'MANUAL');self.assertTrue(c.permits('user_explicit'));self.assertFalse(c.permits('character_auto'))

    def test_proactive_requires_parent_and_preference(self):
        for parent in ('OFF','MANUAL','ASSISTED'):
            self.assertFalse(registry.resolve({'capabilities':{'image':parent,'proactive_image':'AUTO'},'proactive_enabled':True},'proactive_image').permits('character_auto'))
        self.assertFalse(registry.resolve({'capabilities':{'image':'AUTO','proactive_image':'AUTO'},'proactive_enabled':False},'proactive_image').permits('character_auto'))

    def test_invalid_persisted_modes_fail_closed(self):
        for mode in ('UNKNOWN',[],{}):
            self.assertFalse(registry.resolve({'capabilities':{'image':mode}}).permits('user_explicit'))

    def test_revision_and_dashboard_match_authorization(self):
        on={'capabilities':{'image':'AUTO'}};off={'capabilities':{'image':'OFF'}}
        self.assertNotEqual(registry.resolve(on).config_revision,registry.resolve(off).config_revision)
        self.assertFalse(image_mode.effective(off)['explicit_requests'])

    def test_preferences_preserve_other_capabilities_and_legacy_command(self):
        with tempfile.TemporaryDirectory() as root,patch.object(preferences,'PATH',Path(root)/'preferences.json'):
            preferences.save({'capabilities':{'image':'OFF','reaction':'OFF'}})
            preferences.save({'capabilities':{'image':'AUTO'}})
            self.assertEqual(preferences.load()['capabilities'],{'image':'AUTO','reaction':'OFF'})
            preferences.save({'spontaneous_images':False})
            self.assertEqual(preferences.load()['capabilities']['image'],'MANUAL')
            with self.assertRaises(ValueError):preferences.save({'capabilities':{'image':[]}})

class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_chat_does_not_emit_a_drawing_failure(self):
        instance=object.__new__(bot.Bot);instance.image_decision_record=Mock();data={}
        with patch.object(preferences,'load',return_value={'capabilities':{'image':'MANUAL'}}):
            await instance.start_drawing({},'k','聊聊天',{},data,{},False)
        self.assertNotIn('_image_failure',data)

    async def test_off_rejects_before_planning_and_gpu(self):
        instance=object.__new__(bot.Bot);instance.image_decision_record=Mock()
        job=Mock();data={}
        with patch.object(preferences,'load',return_value={'capabilities':{'image':'OFF'}}):
            await instance.start_drawing({'_pending_image_job':job},'k','画一张图',{},data,{},True)
        job.record.assert_called_once_with('cancelled',reason='capability_off')
        self.assertEqual(data['_image_failure'],'capability_off')
        self.assertEqual(instance.image_decision_record.call_args.args[1]['effective_mode'],'OFF')

class QueueRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_while_waiting_prevents_comfy_submission(self):
        import image_director
        with tempfile.TemporaryDirectory() as folder,patch.object(bot,'ROOT',Path(folder)):
            instance=bot.Bot(None);instance.connected.set();instance.call=AsyncMock()
            instance.comfy.image=AsyncMock(return_value=b'not-a-real-image')
            key='private:0:42';session=instance.store.get(key)
            settings={**preferences.load(),'capabilities':{'image':'MANUAL'}}
            job=image_director.Job(Path(folder)/'jobs',{'hairstyle':''}, {'kind':'explicit','priority':0},'prompt',0)
            try:
                with patch.object(preferences,'load',side_effect=lambda:copy.deepcopy(settings)):
                    async with instance.image_slots.slot(0):
                        task=asyncio.create_task(instance.render_live({'message_type':'private','user_id':42},key,'prompt',[],session['state'],0,job=job))
                        for _ in range(50):
                            if instance.image_slots.waiters:break
                            await asyncio.sleep(.001)
                        self.assertTrue(instance.image_slots.waiters)
                        settings['capabilities']['image']='OFF'
                    await asyncio.wait_for(task,1)
                instance.comfy.image.assert_not_awaited();instance.call.assert_not_awaited()
                self.assertEqual(job.data['status'],'cancelled')
            finally:instance.store.db.close()

if __name__=='__main__':unittest.main()
