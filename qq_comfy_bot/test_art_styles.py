import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import art_styles as styles
import bot
import image_workflow as workflow
from session_store import SessionStore
import test_upgrade
import test_bot

ROOT=Path(__file__).parent

class RecipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.graph=json.loads((ROOT/'workflow_h_api.json').read_text(encoding='utf-8-sig'))
        self.snapshot=dict(character_id='test',outfit='white shirt',hairstyle='long hair',style='life',
            identity='adult woman, violet hair, golden eyes',outfit_tags='white shirt',setting_tags='room',
            action='reading',props='book',emotion='calm',camera='medium shot',local_time='2026-11-07T12:00:00+08:00')

    async def test_frozen_recipes_replace_old_models_loras_and_sampler(self):
        original=copy.deepcopy(self.graph)
        for style in ('cel','handdrawn'):
            profile=workflow.prepare(ROOT,{**self.snapshot,'art_style':style})
            graph=await workflow.apply(None,'',self.graph,profile)
            self.assertEqual(graph['66']['inputs']['unet_name'],'miaomiaoHarem_29BBETA10.safetensors')
            self.assertEqual(graph['70']['inputs']['cfg'],1.6)
            self.assertEqual(graph['70']['inputs']['steps'],30)
            self.assertEqual(graph['70']['inputs']['denoise'],.9)
            self.assertEqual(graph['70']['inputs']['sampler_name'],'euler')
            self.assertEqual(graph['68']['inputs'],dict(width=896,height=1152,batch_size=1))
            self.assertEqual([(graph[k]['inputs']['lora_name'],graph[k]['inputs']['strength_model']) for k in ('107','109','150')],styles.STYLES[style]['loras'])
            self.assertNotIn('151',graph);self.assertNotIn('152',graph)
            self.assertEqual(graph['9']['inputs']['images'],['style_hands',0])
            self.assertEqual(graph['style_hands']['class_type'],'TulutHandDetailer')
            self.assertIn('multiple panels',graph['71']['inputs']['text'])
        self.assertEqual(original,self.graph)
        original_profile=workflow.prepare(ROOT,self.snapshot)
        restored=await workflow.apply(None,'',self.graph,original_profile)
        self.assertEqual(restored['66']['inputs'],original['66']['inputs'])
        self.assertEqual(restored['70']['inputs']['cfg'],5.)

    def test_prompt_style_does_not_leak_original_artists_or_change_identity(self):
        for style in ('cel','handdrawn'):
            prompt=bot.visual_prompt('violet hair, golden eyes, white shirt, @92m, @BlueArchStyle, (@rurudo:1.9)',12,art_style=style)
            self.assertIn('@92m',prompt);self.assertIn('@BlueArchStyle',prompt)
            self.assertNotIn('@rurudo',prompt);self.assertNotIn('@meme50',prompt)
            self.assertIn('violet hair',prompt);self.assertIn('white shirt',prompt)
        self.assertNotIn('@92m',bot.visual_prompt('@92m',12))

    async def test_nonstandard_node_ids_and_original_dimensions(self):
        mapping={'65':'184','71':'191'}
        graph={mapping.get(k,k):copy.deepcopy(n) for k,n in self.graph.items()}
        for node in graph.values():
            for v in node['inputs'].values():
                if isinstance(v,list) and len(v)==2:v[0]=mapping.get(v[0],v[0])
        result=await workflow.apply(None,'',graph,dict(width=608,height=976,steps=30,cfg=1.6,shot_plan={'size':'close','direction':'front'}))
        self.assertEqual(result['9']['inputs']['images'],['184',0])
        self.assertNotIn('cropped',result['191']['inputs']['text'])
        self.assertNotIn('180',result)
        for node in result.values():
            for v in node['inputs'].values():
                if isinstance(v,list) and len(v)==2:self.assertIn(v[0],result)

    def test_modes_and_avatar_landscape_remain_independent(self):
        for mode in ('comfort','comic'):
            snapshot={**self.snapshot,'style':mode,'art_style':'cel'}
            profile=workflow.prepare(ROOT,snapshot)
            prompt=workflow.prompt(snapshot,profile,'legacy artists')
            self.assertIn('chibi',prompt);self.assertIn('@92m',prompt)
            self.assertEqual((profile['width'],profile['height']),(576,832))
        profile=workflow.prepare(ROOT,{**self.snapshot,'art_style':'cel','shot_plan':{'orientation':'landscape'}})
        self.assertEqual((profile['width'],profile['height']),(1152,896))

class CommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_upgrade.EntryTests.asyncSetUp
    asyncTearDown=test_upgrade.EntryTests.asyncTearDown
    workers=test_upgrade.EntryTests.workers
    image_data=test_upgrade.EntryTests.image_data

    async def command(self,text):
        self.instance.enqueue(self.event,self.key,text)
        await self.workers()

    async def test_commands_do_not_call_model_preserve_state_persist_and_isolate(self):
        baseline=self.instance.store.get(self.key)
        with patch.object(bot,'deepseek',AsyncMock()) as model:
            await self.command('/画风')
            await self.command('/画风 赛璐璐')
            await self.command('/画风 不存在')
            await self.command('/状态')
            model.assert_not_called()
        session=self.instance.store.get(self.key)
        self.assertEqual(session['art_style'],'cel')
        self.assertEqual(session['state'],baseline['state'])
        self.assertEqual(styles.selected(self.instance.store.get('other')),'original')
        db_path=self.instance.store.db.execute('PRAGMA database_list').fetchone()[2]
        reopened=SessionStore(db_path,{})
        try:self.assertEqual(reopened.get(self.key)['art_style'],'cel')
        finally:reopened.db.close()
        await self.command('/画风 原版')
        self.assertEqual(styles.selected(self.instance.store.get(self.key)),'original')

    async def test_inflight_job_keeps_frozen_style_when_session_switches(self):
        await self.command('/画风 赛璐璐')
        started=asyncio.Event();release=asyncio.Event();seen=[]
        async def draw(prompt,job=None):
            seen.append(copy.deepcopy(job.data));started.set();await release.wait();return b'image'
        self.instance.comfy.image=draw
        with patch.object(bot,'deepseek',AsyncMock(return_value=self.image_data())),patch.object(bot.image_director,'inspect_image',AsyncMock(return_value={'pass':True})):
            self.instance.enqueue(self.event,self.key,'画一张')
            await asyncio.wait_for(started.wait(),3)
            task=self.instance.image_tasks[self.key]
            await self.command('/画风 手绘')
            self.assertFalse(task.done())
            release.set();await asyncio.wait_for(task,3)
        self.assertEqual(seen[0]['render_config']['art_style'],'cel')
        self.assertEqual(seen[0]['snapshot']['art_style'],'cel')
        self.assertNotIn('@rurudo',seen[0]['prompt'])
        self.assertEqual(styles.selected(self.instance.store.get(self.key)),'handdrawn')

class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_bot.APITests.asyncSetUp
    asyncTearDown=test_bot.APITests.asyncTearDown

    async def test_missing_preset_lora_is_rejected_before_any_gpu_submission(self):
        from unittest.mock import Mock
        comfy=bot.Comfy(self.http)
        job=Mock();job.data={'render_config':styles.settings('cel')};job.id='test-style-validation'
        with self.assertRaisesRegex(RuntimeError,'模型文件名|选项不可用'):
            await comfy._image('adult woman, reading in a room',job)
        self.assertEqual(self.requests,[])
