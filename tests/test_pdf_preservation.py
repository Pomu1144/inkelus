import base64
import io
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pymupdf

from vscode_assistant import engine, pdfinput
from vscode_assistant.library import Library
from vscode_assistant.server import Handler


def source_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), 'Left column: original layout')
    page.insert_text((320, 60), 'Right column: table')
    page.draw_rect(pymupdf.Rect(320, 80, 520, 140))
    page.insert_text((330, 100), 'Revenue       120')
    page = doc.new_page()
    page.insert_text((50, 60), 'Second page: diagram')
    page.draw_circle((200, 200), 60, color=(0, 0, 1))
    return doc.tobytes()


class PDFPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = Library(os.path.join(self.temp.name, 'data'))
        self.source = os.path.join(self.temp.name, 'Homework.pdf')
        self.data = source_pdf()
        with open(self.source, 'wb') as stream:
            stream.write(self.data)
        self.name = self.lib.import_file('documents', self.source)

    def test_original_is_pdf_and_byte_identical_with_separate_text_cache(self):
        self.assertEqual(self.name, 'Homework.pdf')
        with open(self.lib.path('documents', self.name), 'rb') as stream:
            self.assertEqual(stream.read(), self.data)
        self.assertIn('Left column', self.lib.read('documents', self.name))
        self.assertTrue(os.path.isfile(self.lib.text_cache('documents', self.name)))
        with self.assertRaises(ValueError):
            self.lib.save('documents', self.name, 'replacement text')
        renamed = self.lib.rename('documents', self.name, 'Renamed.pdf')
        self.assertIn(renamed, self.lib.pdf_originals([renamed]))
        with open(self.lib.path('documents', renamed), 'rb') as stream:
            self.assertEqual(stream.read(), self.data)

    def test_scanned_pdf_can_be_imported_without_extractable_text(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(30, 30, 100, 100), fill=(1, 0, 0))
        path = os.path.join(self.temp.name, 'Scan.pdf')
        doc.save(path)
        name = self.lib.import_file('documents', path)
        reader = pdfinput.Reader(self.lib.pdf_originals([name], max_bytes=None))
        self.assertEqual(reader.counts[name], 1)
        self.assertTrue(base64.b64decode(reader.message()['images'][0]).startswith(b'\x89PNG'))

    def test_legacy_originals_and_page_selection_bounds(self):
        self.lib.save('documents', 'Legacy.md', 'flattened text')
        path = self.lib.original('documents', 'Legacy.md')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as stream:
            stream.write(self.data)
        reader = pdfinput.Reader(self.lib.pdf_originals(['Legacy.md'], max_bytes=None))
        reader.load('Legacy.md', [2])
        self.assertEqual(len(reader.message()['images']), 2)
        for name, pages in [('Legacy.md', [0]), ('Legacy.md', [3]), ('Legacy.md', [True]), ('other.pdf', [1])]:
            with self.assertRaises(ValueError):
                reader.load(name, pages)

    def run_turn(self, provider, conn_type='ollama', mode='visual'):
        store = SimpleNamespace(dir=self.temp.name, settings={'web': {'max_steps': 3}, 'defaults': {}})
        chat = {'id': 'chat123', 'model': 'local::test', 'params': {}, 'pdf_mode': mode,
                'docs': [self.name], 'messages': [{'role': 'user', 'content': 'Explain page 2'}]}
        with patch.object(engine.providers, 'create', return_value=provider):
            engine.run_turn(store, self.lib, chat, {'type': conn_type}, 'test', threading.Event(),
                            lambda *a: None, lambda *a: self.fail('Unexpected confirmation'), None)
        return chat

    def test_local_vision_receives_images_and_can_request_next_page(self):
        test = self
        name = self.name
        class Provider:
            count = 0
            def show(self, model): return {'capabilities': ['vision', 'tools']}
            def chat(self, model, history, params, delta, cancel, tools=None, **kwargs):
                self.count += 1
                test.assertIn('PDF inventory', history[0]['content'])
                test.assertNotIn('Left column: original layout', history[0]['content'])
                test.assertEqual(len(history[-1]['images']), self.count)
                if self.count == 1:
                    test.assertIn('read_pdf_pages', [t['function']['name'] for t in tools])
                    return [{'id':'pages', 'name':'read_pdf_pages', 'arguments':{'document':name,'pages':[2]}}], {}
                delta('Page 2 has a diagram.', '')
                return [], {}
        chat = self.run_turn(Provider())
        self.assertEqual(chat['messages'][-1]['content'], 'Page 2 has a diagram.')
        self.assertNotIn('images', chat['messages'][-1])

    def test_nonvision_model_requires_explicit_text_mode(self):
        provider = SimpleNamespace(show=lambda _: {'capabilities':['completion']}, chat=Mock())
        chat = self.run_turn(provider)
        self.assertIn('cannot see PDF layout', chat['messages'][-1]['error'])
        provider.chat.assert_not_called()
        seen = []
        def reply(model, history, params, delta, cancel, **kwargs):
            seen.extend(history)
            delta('Text-only answer', '')
            return [], {}
        provider.chat = reply
        self.run_turn(provider, mode='text')
        self.assertTrue(any('TEXT ONLY' in m['content'] for m in seen))
        self.assertTrue(any('Left column' in m['content'] for m in seen))
        self.assertFalse(any(m.get('images') for m in seen))

    def test_native_provider_failure_does_not_silently_flatten_pdf(self):
        provider = SimpleNamespace(chat=Mock(side_effect=RuntimeError('document rejected')))
        chat = self.run_turn(provider, conn_type='bedrock')
        self.assertEqual(provider.chat.call_count, 1)
        self.assertEqual(provider.chat.call_args.args[2]['documents'][0]['data'], self.data)
        self.assertIn('PDF layout input failed', chat['messages'][-1]['error'])

    def test_original_pdf_endpoint_returns_source_bytes(self):
        handler = object.__new__(Handler)
        handler.hub = SimpleNamespace(library=self.lib)
        handler.send = Mock()
        handler.api_library('GET', ['documents', 'original'], {'name':[self.name]})
        self.assertEqual(handler.send.call_args.args[1], self.data)
        self.assertEqual(handler.send.call_args.args[2], 'application/pdf')

    def test_binary_pdf_import_preserves_bytes_without_extraction(self):
        handler = object.__new__(Handler)
        handler.hub = SimpleNamespace(library=self.lib, lock=threading.RLock())
        handler.headers = {'Content-Type': 'application/pdf', 'Content-Length': str(len(self.data))}
        handler.rfile = io.BytesIO(self.data)
        handler.ok = Mock()
        handler.fail = Mock()
        with patch.object(self.lib, 'read', side_effect=AssertionError('Import must not extract text')):
            handler.api_library('POST', ['documents', 'import'], {'filename': ['New PDF.pdf']})
        handler.fail.assert_not_called()
        name = handler.ok.call_args.args[0]['name']
        self.assertEqual(name, 'New PDF.pdf')
        with open(self.lib.path('documents', name), 'rb') as stream:
            self.assertEqual(stream.read(), self.data)
        self.assertFalse(os.path.isfile(self.lib.text_cache('documents', name)))

    def test_binary_pdf_import_rejects_oversize_or_truncated_data(self):
        handler = object.__new__(Handler)
        handler.hub = SimpleNamespace(library=self.lib, lock=threading.RLock())
        handler.headers = {'Content-Type': 'application/pdf', 'Content-Length': str(61 * 1024 * 1024)}
        handler.fail = Mock()
        handler.api_library('POST', ['documents', 'import'], {'filename': ['Too big.pdf']})
        self.assertEqual(handler.fail.call_args.args[0], 400)
        handler.headers['Content-Length'] = str(len(self.data) + 1)
        handler.rfile = io.BytesIO(self.data)
        with self.assertRaisesRegex(ValueError, 'ended early'):
            handler.api_library('POST', ['documents', 'import'], {'filename': ['Incomplete.pdf']})
        self.assertNotIn('Incomplete.pdf', self.lib.names('documents'))

    def test_wire_payloads_include_png_images_for_both_local_provider_types(self):
        from vscode_assistant.providers import Ollama, OpenAICompat
        reader = pdfinput.Reader(self.lib.pdf_originals([self.name], max_bytes=None))
        image = reader.message()['images'][0]
        ollama = Ollama({'url':'http://localhost:11434'})
        compatible = OpenAICompat({'url':'http://localhost:1234'})
        self.assertEqual(ollama._wire([reader.message()])[0]['images'][0], image)
        self.assertEqual(compatible._wire([reader.message()])[0]['content'][1]['image_url']['url'], 'data:image/png;base64,' + image)


if __name__ == '__main__':
    unittest.main()
