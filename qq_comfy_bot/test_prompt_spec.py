import copy,json,tempfile,unittest
from pathlib import Path
import prompt_spec as p,shot_director as shots,image_workflow as w,image_concept as concept
from test_bot import workflow_info
import bot

def scene():
    return dict(character_id='hana',identity='suzuki hana, 1woman, solo, golden eyes, long blue-violet hair',
        outfit='淡蓝长裙',hairstyle='long blue-violet hair',style='life',art_style='original',
        outfit_tags='pale blue long dress, white slippers, stirrup socks, blonde hair',
        location='家里',setting_tags='home room, desk by window, soft midday light',
        action='holding a pen above a notebook',props='a red umbrella',emotion='gentle smile',
        local_time='2026-11-07T12:00:00+08:00',creative_brief={})

CASES=[('窗边自拍','one hand raised in a peace sign, other hand on hip','home window','半身'),
 ('书桌写字','holding a pen above a notebook','home desk','半身'),
 ('奶茶店手机','holding one smartphone with both hands','bubble tea shop','半身'),
 ('厨房','stirring soup with a spoon','home kitchen','半身'),
 ('玄关','adjusting one shoe, kneeling by the door','home entryway, shoe cabinet','全身'),
 ('夜间','reading a book','home room','半身'),
 ('户外','walking on a path','outdoors, park','全身'),
 ('安慰','offering a warm cup','imagined quiet corner','半身'),
 ('漫画','making a silly surprised expression','imagined room','半身'),
 ('全身','one leg lifted, one hand on hip','home room','全身'),
 ('手部','one hand turning a page, the other holding a pen','one wooden desktop','手部特写'),
 ('面部','holding a paper near her lips, one hand resting on lower ribs','home window','面部近景')]

class CompilerTests(unittest.IsolatedAsyncioTestCase):
    def test_twelve_scene_text_fixtures(self):
        for i,(name,action,setting,request) in enumerate(CASES):
            with self.subTest(name=name):
                s=scene();s.update(action=action,setting_tags=setting)
                if i==5:s['local_time']='2026-11-07T22:00:00+08:00'
                if i in (7,8):s['style']='comfort' if i==7 else 'comic'
                s['shot_plan']=shots.choose(s,request)
                spec=p.build(s);text=spec.positive()
                self.assertEqual(text.count(spec.camera),1)
                if spec.action:self.assertEqual(text.count(spec.action),1)
                if spec.expression:self.assertEqual(text.count(spec.expression),1)
                self.assertNotIn('blonde hair',text)
                self.assertEqual(text.count('ambient artificial lighting' if i==5 else 'natural daylight'),1)
                if s['shot_plan']['expectations']['eyes_expected']:self.assertIn('golden eyes',text)
                else:self.assertNotIn('golden eyes',text)
                self.assertIn('red umbrella',text)
                if i==10:
                    self.assertNotIn('long dress',text);self.assertNotIn('hair',spec.outfit)
                if i==11:
                    self.assertNotIn('lower ribs',text);self.assertNotIn('slippers',text)
                if i==7:self.assertIn('colored pencil',text)
                if i==8:self.assertIn('manga',text)

    def test_action_constraints_before_diversity_and_user_override(self):
        s=scene();s['action']='one leg lifted, weight on one foot'
        self.assertIn(shots.choose(s)['size'],('full','wide','extreme_wide'))
        self.assertEqual(shots.choose(s,'侧脸特写')['size'],'close')
        s['shot_plan']=shots.choose(s,'侧脸特写')
        self.assertNotIn('foot',p.build(s).action)

    def test_no_book_from_camera_no_props_loss(self):
        s=scene();s['shot_plan']=shots.choose(s,'越肩半身')
        text=p.build(s).positive();self.assertNotIn('looking at the book',text)
        self.assertIn('red umbrella',text)

    def test_scene_conflict_and_hair_cache_are_not_silent(self):
        self.assertIn('saved_location_conflict',concept.conflicts(dict(state={'location':'家里'},visual_scene={'setting':'dance rehearsal room'},image_plan={'action':'reading'})))
        s=scene();s['shot_plan']=shots.choose(s,'半身');self.assertNotIn('blonde hair',p.build(s).positive())
        self.assertEqual(s['outfit_tags'],scene()['outfit_tags'])

    async def test_exact_compiler_output_into_both_workflows_and_immutable_settings(self):
        root=Path(__file__).parent
        graphs=[json.loads((root/'workflow_h_api.json').read_text(encoding='utf-8-sig'))]
        ui=json.loads((root/'workflow.json').read_text(encoding='utf-8-sig'))
        graphs.append(bot.convert(ui,workflow_info(ui)))
        for graph in graphs:
            s=scene();s['shot_plan']=shots.choose(s,'面部近景')
            cfg=w.prepare(root,s);text=w.prompt(s,cfg);before=copy.deepcopy(graph)
            graph['67']['inputs']['text']=text
            expected=p.PromptSpec(**cfg['prompt_spec']).negative(graph['71']['inputs']['text'])
            result=await w.apply(None,'',graph,cfg)
            self.assertEqual(result['67']['inputs']['text'],text)
            self.assertEqual(result['71']['inputs']['text'],expected)
            for node in ('66','62','63','69'):
                self.assertEqual(result[node],before[node])
            self.assertEqual(result['70']['inputs']['sampler_name'],before['70']['inputs']['sampler_name'])
            self.assertIn('multiple panels',expected)

    def test_whole_action_is_not_repeated_from_gesture(self):
        from test_planning_fixture import creative
        b=creative();b['action']='holding a pen above a notebook';b['gesture']=b['action']
        d={'creative_brief':b,'visual_scene':{}}
        concept.apply(d);self.assertEqual(d['visual_scene']['action'],b['action'])
        b['gesture']='turning her head toward the viewer'
        d={'creative_brief':b,'visual_scene':{}}
        concept.apply(d);self.assertIn('turning her head',d['visual_scene']['action'])


    def test_preserves_designed_light_camera_material_and_reference_style(self):
        s=scene()
        s.update(art_style='handdrawn', outfit_tags='pale blue dress, long blue-purple hair loose')
        s['creative_brief']={'atmosphere':'soft reflected light on skin',
            'art_direction':{'lighting':'warm window rim lighting, cool bounce light',
                'material_details':'matte folds in the dress fabric',
                'camera_relationship':'view across the corner of the tabletop'}}
        s['shot_plan']=copy.deepcopy(next(shot for shot in shots.SHOTS if shot['id']=='table_level'))
        text=p.build(s).positive()
        for detail in ('@92m','masterpiece','soft reflected light on skin','warm window rim lighting',
                       'cool bounce light','matte folds in the dress fabric','tabletop height',
                       'foreground activity props','view across the corner'):
            self.assertIn(detail,text)
        self.assertEqual(p.build(s).outfit,'pale blue dress')

    def test_night_and_user_camera_lock_take_precedence(self):
        s=scene()
        s.update(local_time='2026-11-07T22:00:00+08:00',
                 setting_tags='home room, desk by the window, direct sunlight')
        s['creative_brief']={'art_direction':{'lighting':'bright sunlight, warm reflected lamplight',
                          'camera_relationship':'high angle looking down from the ceiling',
                          'pose_geometry':'one hand resting on her knee'}}
        s['shot_plan']=shots.choose(s,'面部近景')
        text=p.build(s).positive()
        self.assertIn('warm reflected lamplight',text)
        self.assertIn('desk by the window',text)
        for contradiction in ('bright sunlight','direct sunlight','looking down from the ceiling','her knee'):
            self.assertNotIn(contradiction,text)

    def test_art_direction_is_optional_and_preserved_by_existing_planner(self):
        from test_planning_fixture import creative
        brief=creative()
        self.assertTrue(concept.validate(brief))
        brief['art_direction']={'lighting':'warm sidelight', 'visual_focus':['not text'], 'unknown':'ignored'}
        data={'creative_brief':brief}
        self.assertTrue(concept.apply(data))
        self.assertEqual(data['creative_brief']['art_direction'],{'lighting':'warm sidelight'})

    async def test_blank_borders_block_even_without_external_review(self):
        import io
        from PIL import Image
        import image_director
        image=Image.new('RGB',(400,600),(196,196,203))
        image.paste('darkblue',(0,220,200,380))
        image.paste('darkgoldenrod',(200,220,400,380))
        buffer=io.BytesIO()
        image.save(buffer,format='PNG')
        result=await image_director.inspect_image(None,buffer.getvalue(),{},'.',{'qa_enabled':False})
        self.assertFalse(result['pass'])
        self.assertEqual(result['status'],'excessive_blank_borders')


    def test_clean_background_keeps_location_action_and_light(self):
        s=scene()
        s.update(setting_tags='home room, desk by window, crowded shelves, wall decorations',
                 creative_brief={'art_direction':{'lighting':'warm reflected window light',
                     'foreground_background':'vases on shelves filling the foreground'}})
        s['shot_plan']=shots.choose(s)
        self.assertIn(s['shot_plan']['size'],('medium','close'))
        spec=p.build(s)
        text=spec.positive()
        for term in ('home room','desk by window','holding a pen','warm reflected window light','simple background'):
            self.assertIn(term,text)
        for term in ('crowded shelves','wall decorations','vases','scnr'):
            self.assertNotIn(term,text)
        self.assertIn('background clutter',spec.negative(''))
        self.assertLess(text.index('best quality'),text.index('suzuki hana'))
        self.assertLess(text.index('suzuki hana'),text.index('@ningen'))
        self.assertLess(text.index('@ningen'),text.index('desk by window'))

    def test_explicit_scenery_can_keep_background_details(self):
        s=scene()
        s['setting_tags']='home room, desk by window, oak bookshelves, patterned wallpaper'
        s['shot_plan']=shots.choose(s,'用远景，背景细节丰富')
        self.assertEqual(s['shot_plan']['size'],'wide')
        spec=p.build(s)
        self.assertIn('patterned wallpaper',spec.positive())
        self.assertNotIn('background clutter',spec.negative(''))
        s['shot_plan']=shots.choose(s,'半身，背景不要太复杂')
        self.assertTrue(s['shot_plan']['simple_background'])
