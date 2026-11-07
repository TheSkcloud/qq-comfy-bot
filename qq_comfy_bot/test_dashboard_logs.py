import json
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
from web_manager import Dashboard


class DashboardLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_use_authenticated_allowlisted_event_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dashboard = Dashboard(root, token='test-console')
            metrics = root / 'data' / 'delivery_metrics.jsonl'
            metrics.write_text(json.dumps(dict(at='2026-11-07T12:00:00+08:00', event='sent',
                elapsed_seconds=1.25, message='private message', token='private token')) + '\n{partial', encoding='utf-8')
            job = root / 'data' / 'image_jobs' / '1234567890' / 'job.json'
            job.parent.mkdir(parents=True)
            job.write_text(json.dumps(dict(prompt='private prompt', events=[dict(at=1794024001,
                status='submitted', reason='gpu_submit', elapsed_ms=500)])), encoding='utf-8')
            async with TestClient(TestServer(dashboard.app())) as client:
                response = await client.get('/api/logs')
                self.assertEqual(response.status, 401)
                headers = {'Authorization': 'Bearer test-console', 'Host': '127.0.0.1'}
                response = await client.get('/api/logs?stream=reply', headers=headers)
                self.assertEqual(response.status, 200)
                rows = (await response.json())['entries']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['elapsed_ms'], 1250)
                self.assertNotIn('private', json.dumps(rows))
                response = await client.get('/api/logs?stream=drawing', headers=headers)
                self.assertEqual((await response.json())['entries'][0]['task'], '12345678')
                response = await client.get('/api/logs?stream=invalid', headers=headers)
                self.assertEqual(response.status, 400)
