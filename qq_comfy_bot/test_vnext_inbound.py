import unittest
from unittest.mock import Mock
import bot

class InboundProtectionTests(unittest.TestCase):
    def instance(self):
        b = object.__new__(bot.Bot)
        b.accept = Mock(return_value=('private:0:1', 'hello'))
        b.enqueue = Mock()
        b.inbox = {}
        b.seen = {}
        b.tasks = set(range(30))
        return b

    def test_fifty_inputs_with_background_jobs(self):
        b = self.instance()
        for i in range(50):
            b.dispatch_inbound({'message_id': i}, None)
        self.assertEqual(b.enqueue.call_count, 50)

    def test_overload_rejected_and_control_allowed(self):
        b = self.instance()
        b.inbox['private:0:1'] = [None] * 100
        b.seen['private:0:1:7'] = 1
        b.dispatch_inbound({'message_id': 7}, None)
        b.enqueue.assert_not_called()
        self.assertNotIn('private:0:1:7', b.seen)
        b.accept.return_value = ('private:0:1', '/取消')
        b.dispatch_inbound({}, None)
        b.enqueue.assert_called_once()

    def test_bad_event_does_not_stop_next_event(self):
        b = self.instance()
        b.accept.side_effect = [TypeError('bad event'), ('private:0:1', 'hello')]
        b.dispatch_inbound({}, None)
        b.dispatch_inbound({}, None)
        b.enqueue.assert_called_once()
