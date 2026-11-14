"""Regression checks for SnowLuma-only config, evidence and image authorization.

These tests use temporary files and never connect to real QQ, DeepSeek or ComfyUI.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import configure
import image_director
from life import Life

NOW = datetime(2026, 11, 14, 20, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


class SnowLumaConfigurationTests(unittest.TestCase):
    def test_save_updates_only_local_bot_and_preserves_legacy_files(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root / '.env.example').write_text(
                'DEEPSEEK_API_KEY=old\nALLOWED_USERS=100001\n'
                'NAPCAT_TOKEN=legacy-token\nNAPCAT_WS_URL=ws://127.0.0.1:3001\n',
                encoding='utf-8')
            (root / 'launcher.json').write_text(
                json.dumps({'qq_provider': 'napcat', 'bot_qq': '500002', 'python':'python'}),
                encoding='utf-8')
            # Regression guard: the new configurator cannot touch old transport configs.
            legacy = root.parent / 'do-not-touch-onebot.json'
            try:
                legacy.write_text('{"sentinel":true}', encoding='utf-8')
                with patch.object(configure, 'ROOT', root):
                    configure.save_config('sk-test', '100001', '500002', 'snow-token',
                                          'ws://127.0.0.1:3001')
                env = (root / '.env').read_text(encoding='utf-8')
                self.assertIn('ONEBOT_TOKEN=snow-token', env)
                self.assertNotIn('NAPCAT_TOKEN=', env)
                self.assertEqual(json.loads((root / 'launcher.json').read_text())['qq_provider'], 'snowluma')
                self.assertEqual(legacy.read_text(encoding='utf-8'), '{"sentinel":true}')
            finally:
                legacy.unlink(missing_ok=True)

    def test_invalid_remote_onebot_endpoint_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            (root/'.env.example').write_text('',encoding='utf-8')
            with patch.object(configure, 'ROOT',root):
                with self.assertRaisesRegex(ValueError,'本机'):
                    configure.save_config('sk-test','100001',onebot_url='ws://example.com:1234')


class MemoryBoundaryTests(unittest.TestCase):
    def test_fabricated_value_not_saved(self):
        with tempfile.TemporaryDirectory() as home:
            life=Life(home)
            message='我今天在图书馆看书'
            stats={}
            accepted=life.record('42',[{'kind':'activity',
                                        'value':'凌晨三点还在加班',
                                        'evidence':message}],message,NOW,diagnostics=stats)
            self.assertEqual(accepted,[])
            self.assertEqual(life.facts('42'),[])
            self.assertEqual(stats['reasons'].get('value_not_in_evidence'),1)

    def test_mood_read_must_not_change_last_update(self):
        with tempfile.TemporaryDirectory() as home:
            life=Life(home)
            life.mood('42',{},NOW)
            with life.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM feelings').fetchone()[0],0)
            life.mood('42',{'joy':2},NOW)
            with life.db() as db:
                before=db.execute('SELECT updated FROM feelings WHERE user=?',('42',)).fetchone()[0]
            life.mood('42',{},NOW+timedelta(hours=12))
            with life.db() as db:
                after=db.execute('SELECT updated FROM feelings WHERE user=?',('42',)).fetchone()[0]
            self.assertEqual(before,after)


class ImageAuthorizationTests(unittest.TestCase):
    def test_auto_activity_cannot_escalate_to_explicit(self):
        session={'state':{'character':'羽那','location':'书房','clothing':'家居服'},
                 'history':[]}
        candidate={'source_type':'life','character_activity':'拍照',
                   'scene_description':dict(session['state']),
                   'expires_at':(NOW+timedelta(minutes=10)).isoformat(),
                   'dedupe_key':'0'*64,'candidate_id':'test'}
        result=image_director.decide(session,'拍照',{'_candidate':candidate},NOW,{},enabled=False)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['kind'],'life')
        self.assertNotEqual(result['reason'],'explicit_user_request')

    def test_actual_user_explicit_photo_still_allowed(self):
        session={'state':{'character':'羽那','location':'书房','clothing':'家居服'},
                 'history':[]}
        result=image_director.decide(session,'/拍照',{'_explicit_image_request':True},NOW,{},enabled=False)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['kind'],'explicit')


if __name__ == '__main__':
    unittest.main()
