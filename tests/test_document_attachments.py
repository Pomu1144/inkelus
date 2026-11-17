import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from vscode_assistant.server import Handler


class DocumentAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.chat = {'id': 'chat123', 'docs': [], 'messages': [], 'model': 'ollama::test', 'titled': True}
        self.handler = object.__new__(Handler)
        self.handler.hub = SimpleNamespace(
            chat=lambda _: self.chat, library=SimpleNamespace(names=lambda _: ['source.md', 'other.md'], original=lambda *a: '/nonexistent.pdf'),
            store=SimpleNamespace(settings={}), lock=threading.RLock(), streams={},
            save_chats=Mock(), save_settings=Mock(), start=Mock())
        self.handler.ok = Mock()
        self.handler.fail = Mock()

    def request(self, method, body, sub='documents'):
        self.handler.body = lambda: body
        self.handler.api_chats(method, ['chat123', sub], {})

    def test_add_preserves_existing_and_original_file_metadata(self):
        self.chat['docs'] = ['other.md']
        self.request('POST', {'name': 'source.md', 'filename': 'Homework.pdf', 'size': 1234})
        self.assertEqual(self.chat['docs'], ['other.md', 'source.md'])
        self.assertEqual(self.chat['doc_meta']['source.md']['filename'], 'Homework.pdf')
        self.request('POST', {'name': 'source.md', 'filename': 'Homework.pdf', 'size': 1234})
        self.assertEqual(self.chat['docs'].count('source.md'), 1)

    def test_remove_only_detaches_and_send_retains_historical_cards(self):
        self.request('POST', {'name': 'source.md', 'filename': 'Homework.pdf', 'size': 1234})
        self.request('POST', {'text': 'Summarize this', 'model': 'ollama::test'}, 'send')
        sent = self.chat['messages'][0]
        self.assertEqual(sent['documents'][0]['filename'], 'Homework.pdf')
        self.request('DELETE', {'name': 'source.md'})
        self.assertEqual(self.chat['docs'], [])
        self.assertEqual(sent['documents'][0]['filename'], 'Homework.pdf')
        self.assertIn('source.md', self.handler.hub.library.names('documents'))

    def test_reject_unknown_documents(self):
        self.request('POST', {'name': '../missing.pdf'})
        self.assertEqual(self.handler.fail.call_args.args[0], 404)
        self.assertEqual(self.chat['docs'], [])


if __name__ == '__main__':
    unittest.main()
