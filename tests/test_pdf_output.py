import io
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vscode_assistant import artifacts, engine, server


class PDFOutputTests(unittest.TestCase):
    def test_render_real_multipage_pdf_and_escape_markup(self):
        from pypdf import PdfReader
        data = artifacts.render_pdf('Study Guide', '# Review\n\n' +
                                    'A < B & C > D\n\n- First item\n\n' * 90)
        reader = PdfReader(io.BytesIO(data))
        self.assertGreater(len(reader.pages), 1)
        text = '\n'.join(p.extract_text() for p in reader.pages)
        self.assertIn('A < B & C > D', text)
        self.assertIn('First item', text)

    def test_unique_files_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            a = artifacts.create(directory, 'chat123', 'Title', 'Hello', '../../bad.pdf')
            b = artifacts.create(directory, 'chat123', 'Title', 'Revised')
            self.assertNotEqual(a['id'], b['id'])
            self.assertNotIn('/', a['filename'])
            self.assertTrue(os.path.isfile(artifacts.artifact_path(directory, 'chat123', a['id'])))
            for chat, aid in [('../escape', a['id']), ('chat123', '../../etc/passwd')]:
                with self.assertRaises(ValueError):
                    artifacts.artifact_path(directory, chat, aid)

    def test_invalid_and_unsupported_content(self):
        for content in ['', 'x' * 200001, '\U0001f600']:
            with self.assertRaises(ValueError):
                artifacts.render_pdf('Title', content)

    def test_save_answer_without_model_and_metadata_survives_json(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            chat = {'id': 'chat123', 'title': 'Answer',
                    'messages': [{'role': 'assistant', 'content': '<think>Private reasoning</think>Final answer'}]}
            handler = object.__new__(server.Handler)
            saved = []
            handler.hub = SimpleNamespace(store=SimpleNamespace(dir=directory), streams={},
                                          chat=lambda _: chat, save_chats=lambda: saved.append(json.dumps(chat)))
            handler.body = lambda: {'index': 0}
            responses = []
            handler.ok = lambda data: responses.append(data)
            handler.api_chats('POST', ['chat123', 'pdf'], {})
            restored = json.loads(saved[0])
            artifact = restored['messages'][0]['artifacts'][0]
            from pypdf import PdfReader
            text = PdfReader(artifacts.artifact_path(directory, 'chat123', artifact['id'])).pages[0].extract_text()
            self.assertIn('Final answer', text)
            self.assertNotIn('Private reasoning', text)

    def test_model_creates_pdf_with_browser_off(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SimpleNamespace(dir=directory, settings={'web': {'max_steps': 2}, 'defaults': {}})
            chat = {'id': 'chat123', 'model': 'ollama::test', 'params': {},
                    'messages': [{'role': 'user', 'content': 'Give me a PDF'}]}
            class Provider:
                count = 0
                def chat(self, model, history, params, delta, cancel, tools=None, **kwargs):
                    self.count += 1
                    if self.count == 1:
                        assert 'create_pdf' in [t['function']['name'] for t in tools]
                        return [{'id': 'call1', 'name': 'create_pdf',
                                 'arguments': {'title': 'Test', 'content': '# Review\n\nHello'}}], {}
                    delta('Your PDF is ready.', '')
                    return [], {}
            with patch.object(engine.providers, 'create', return_value=Provider()):
                engine.run_turn(store, None, chat, {'type': 'ollama'}, 'test', threading.Event(),
                                lambda *a: None, lambda *a: self.fail('Unexpected confirmation'), None)
            result = next(m for m in chat['messages'] if m['role'] == 'tool')
            self.assertEqual(result['status'], 'done')
            self.assertTrue(result['artifacts'][0]['size'] > 100)
            # The actual download endpoint requires a matching artifact in this chat.
            handler = object.__new__(server.Handler)
            handler.hub = SimpleNamespace(store=store, chat=lambda _: chat)
            responses = []
            handler.send = lambda *args: responses.append(args)
            handler.fail = lambda *args: responses.append(args)
            aid = result['artifacts'][0]['id']
            handler.api_chats('GET', ['chat123', 'artifacts', aid], {})
            self.assertEqual(responses[-1][0], 200)
            self.assertTrue(responses[-1][1].startswith(b'%PDF-'))
            self.assertEqual(responses[-1][2], 'application/pdf')
            handler.api_chats('GET', ['chat123', 'artifacts', 'f' * 32], {})
            self.assertEqual(responses[-1][0], 404)


if __name__ == '__main__':
    unittest.main()
