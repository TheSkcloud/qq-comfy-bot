import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
import test_remediation
import visual_selector
from test_planning_fixture import creative


def install_catalogs(directory):
    directory.mkdir(parents=True)
    entries = {
        'pose': ('pose_1792200000000', 'Arm Support', 'arm support,'),
        'clothing': ('1741126270296', 'School Uniform & Loafers', 'school uniform, loafers'),
        'background': ('bg_1782250705764', 'Classroom Desk', 'classroom, desk, daytime, clutter'),
    }
    for group, (identifier, name, tags) in entries.items():
        rows = [{'id': identifier, 'name': name, 'tags': tags,
                 'preview': 'https://example.test/preview.webp'}]
        (directory / f'{group}_data.js').write_text('const data = '+json.dumps(rows)+';', encoding='utf-8')


class CatalogTests(unittest.TestCase):
    def test_missing_catalog_is_an_unchanged_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            original = {'style': 'life', 'action': 'resting her forearm on the sill'}
            snapshot = dict(original)
            visual_selector.apply(folder, snapshot, locator=folder)
            self.assertEqual(snapshot, original)

    def test_locked_outfit_location_and_negated_pose_are_respected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'catalog'
            install_catalogs(directory)
            original = {
                'style': 'life', 'action': 'not resting her forearm on the sill',
                'outfit_tags': 'navy school blazer', 'setting_tags': 'classroom, school desk',
                'visual_overrides': ['outfit', 'setting'],
            }
            snapshot = dict(original)
            visual_selector.apply(folder, snapshot, locator=directory)
            self.assertEqual(snapshot, original)


class PipelineCatalogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_remediation.PipelineTests.asyncSetUp
    asyncTearDown = test_remediation.PipelineTests.asyncTearDown

    async def test_selector_is_used_before_compiler_and_frozen_in_the_job(self):
        comfy = self.root / 'comfy'
        install_catalogs(comfy / 'custom_nodes' / 'comfyui-anima-tools' / 'js')
        (self.root / 'launcher.json').write_text(json.dumps({'comfy_cwd': str(comfy)}), encoding='utf-8')
        self.data['visual_scene'] = {
            'identity': 'suzuki hana', 'outfit': 'navy school blazer, white blouse',
            'setting': 'classroom, school desk', 'action': 'resting her forearm on the school desk',
            'composition': 'medium shot',
        }
        self.data['creative_brief'] = creative(self.data['visual_scene']['action'])
        session = self.instance.store.get(self.key)
        with patch.dict('os.environ', {'ANIMA_SELECTOR_CATALOG_DIR': ''}), \
             patch.object(self.instance, 'image_worker', AsyncMock()):
            job = self.instance.launch_image(
                self.event, self.key, '画一张半身照', session, self.data,
                {'allowed': True, 'kind': 'explicit', 'style': 'life'},
                self.transport_preferences, explicit=True,
            )
        self.assertIn('arm support', job.data['prompt'])
        self.assertIn('school uniform', job.data['prompt'])
        self.assertNotIn('loafers', job.data['prompt'])
        self.assertNotIn('clutter, daytime', job.snapshot['setting_tags'])
        self.assertEqual(job.snapshot['visual_selector']['pose']['id'], 'pose_1792200000000')
        self.assertEqual(job.snapshot['visual_selector']['background']['id'], 'bg_1782250705764')
        self.assertEqual(self.instance.store.get(self.key)['state'], bot.DEFAULT)
