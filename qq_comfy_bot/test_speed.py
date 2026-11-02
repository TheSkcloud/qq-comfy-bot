import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
import bot,image_workflow, image_director as d
import test_upgrade

class SpeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_upgrade.EntryTests.asyncSetUp(self)
        self.key_patch=patch.dict(bot.os.environ,{'DEEPSEEK_API_KEY':'unit-test-dummy'})
        self.key_patch.start();self.addCleanup(self.key_patch.stop)
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    image_data=test_upgrade.EntryTests.image_data

    async def test_gpu_starts_while_first_text_is_still_waiting(self):
        started=asyncio.Event();release=asyncio.Event();text_waiting=asyncio.Event()
        async def draw(*a,**kw):started.set();await release.wait();return b'png'
        self.instance.comfy.image=draw
        def remaining(*args,**kwargs):text_waiting.set();return 30
        self.instance.scheduler.remaining=remaining
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())):
            self.instance.enqueue(self.event,self.key,'画一张你在看书的图')
            await asyncio.wait_for(started.wait(),2)
            await asyncio.wait_for(text_waiting.wait(),2)
            self.assertEqual(self.packets,[])
            self.assertFalse(release.is_set())

    async def test_native_save_bypasses_expensive_upscaler(self):
        graph=json.loads((Path(__file__).parent/'workflow_h_api.json').read_text(encoding='utf-8'))
        original=copy.deepcopy(graph)
        out=await image_workflow.apply(None,'',graph,{'width':768,'height':1152,'steps':28,'upscale':False})
        self.assertEqual(out['68']['inputs']['width'],768)
        self.assertEqual(out['68']['inputs']['height'],1152)
        self.assertEqual(out['9']['inputs']['images'],['65',0])
        self.assertNotIn('181',out);self.assertEqual(graph,original)

    async def test_active_chat_can_offer_current_life_without_model_proposing_it(self):
        data=copy.deepcopy(self.data);data['image_value']={'veto':False}
        cfg=bot.preferences.load();cfg['drawing']={**cfg['drawing'],'quiet_start':0,'quiet_end':0}
        with patch.object(bot.preferences,'load',return_value=cfg),patch.dict(bot.os.environ,{'IMAGE_MODE':'auto'}),patch.object(self.instance,'prepare_candidate',AsyncMock()) as prepare:
            await self.instance.start_drawing(self.event,self.key,'你在做什么',self.instance.store.get(self.key),data,{'intent':'casual_chat'},False)
            await asyncio.gather(*list(self.instance.tasks))
        self.assertEqual(prepare.await_count,1)
        self.assertEqual(prepare.call_args.args[2].source_type,'life')

    async def test_active_life_respects_explicit_veto(self):
        data=copy.deepcopy(self.data);data['image_value']={'veto':True}
        with patch.dict(bot.os.environ,{'IMAGE_MODE':'auto'}),patch.object(self.instance,'prepare_candidate',AsyncMock()) as prepare:
            await self.instance.start_drawing(self.event,self.key,'不要发图',self.instance.store.get(self.key),data,{'intent':'casual_chat'},False)
            prepare.assert_not_called()

    async def test_missing_description_does_not_block_chat_model_result(self):
        visual=self.image_data()
        response=AsyncMock();response.status=200
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'reply':'行。','state':{},'send_image':False})}}]}
        context=AsyncMock();context.__aenter__.return_value=response
        http=Mock();http.post.return_value=context
        with patch('image_planner.complete_visual',AsyncMock(return_value=visual)) as recover:
            result=await bot.deepseek(http,copy.deepcopy(bot.DEFAULT),[],'画一张你在看书',True)
        recover.assert_not_called()
        self.assertEqual(result['reply'],'行。')
        self.assertFalse(result['visual_tags'])

    async def test_visual_recovery_accepts_flat_provider_fields(self):
        import image_planner
        fields=self.image_data()['visual_scene']
        response=AsyncMock()
        response.raise_for_status=Mock()
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(fields)}}]}
        context=AsyncMock();context.__aenter__.return_value=response
        http=Mock();http.post.return_value=context
        result=await image_planner.complete_visual(http,'https://example.test',bot.DEFAULT,'画一张看书')
        self.assertEqual(result['visual_scene'],fields)
        self.assertEqual(http.post.call_count,1)

if __name__=='__main__':unittest.main()
