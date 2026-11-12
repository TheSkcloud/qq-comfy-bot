import copy,json,tempfile,unittest
from pathlib import Path
import image_workflow as w

class WorkflowTests(unittest.TestCase):
    def test_explicit_photo_and_topic_change(self):
        from image_policy import image_requested
        self.assertTrue(image_requested('换上黄色连帽衫，拍张照给我'))
        self.assertFalse(image_requested('不聊照片了，讲讲动量守恒'))
        self.assertFalse(image_requested('你的照片真好看'))
    def setUp(self):
        self.snapshot=dict(character_id='card:suzuki_hana',outfit='white home shirt',hairstyle='long blue-violet hair',
                           style='life',identity='golden eyes, long blue-violet hair',outfit_tags='white shirt',
                           setting_tags='room, desk',action='holding water',props='water glass',emotion='calm',
                           camera='medium shot',local_time='2026-11-01T21:00:00+08:00')

    def test_reference_requires_exact_role_outfit_hair_and_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();(root/'ref.png').write_bytes(b'reference')
            (root/'render_profiles.json').write_text(json.dumps({'life':{'reference':True}}))
            anchor={w.signature(self.snapshot):{'path':str(root/'ref.png')}}
            (root/'data'/'reference_anchors.json').write_text(json.dumps(anchor))
            self.assertTrue(w.prepare(root,self.snapshot)['reference'])
            for field in ('character_id','outfit','hairstyle'):
                changed={**self.snapshot,field:'changed'}
                self.assertFalse(w.prepare(root,changed)['reference'])
            self.assertFalse(w.prepare(root,{**self.snapshot,'style':'comfort'})['reference'])

    def test_mode_prompts_separate_doodle_manga_and_reference_scene(self):
        ref=w.prompt(self.snapshot,{'reference':True},'old artist prompt')
        self.assertEqual(ref,'old artist prompt')
        comfort=w.prompt({**self.snapshot,'style':'comfort'},{'reference':False},'legacy')
        comic=w.prompt({**self.snapshot,'style':'comic'},{'reference':False},'legacy')
        self.assertIn('colored pencil',comfort);self.assertIn('manga',comic)
        self.assertIn('white shirt',comfort)

    def test_profiles_do_not_mutate_shared_defaults(self):
        before=copy.deepcopy(w.PROFILES)
        with tempfile.TemporaryDirectory() as folder:
            config=w.prepare(folder,self.snapshot);config['steps']=99
        self.assertEqual(w.PROFILES,before)

if __name__=='__main__':unittest.main()
