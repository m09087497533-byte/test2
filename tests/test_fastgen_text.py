import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fastgen_text as client
from planner import plan_story
from project import Project


def response(value, status=200):
    return Mock(ok=status < 400, status_code=status, text=json.dumps(value), json=Mock(return_value=value))


def chat(content, finish='stop'):
    return response({'choices': [{'message': {'content': content}, 'finish_reason': finish}]})


class TextClientTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(client.photo, 'FASTGEN_API_KEY', 'secret-test-key')
        self.env = patch.dict(os.environ, {'FASTGEN_TEXT_MODEL': 'account/text-model'})
        self.key.start(); self.env.start()
        self.addCleanup(self.key.stop); self.addCleanup(self.env.stop)

    def test_uses_chat_endpoint_and_reads_fenced_content(self):
        with patch.object(client.requests, 'request', return_value=chat('```json\n{"summary":"Рассказ"}\n```')) as request:
            self.assertEqual(client.json_object('Read narration'), {'summary': 'Рассказ'})
        args, kwargs = request.call_args
        self.assertEqual(args, ('POST', client.BASE + '/v1/chat/completions'))
        self.assertEqual(kwargs['json']['model'], 'account/text-model')
        self.assertEqual(kwargs['json']['response_format'], {'type': 'json_object'})
        self.assertNotIn('user_prompt', kwargs['json'])

    def test_unsupported_response_format_retries_without_parameter(self):
        bodies = []
        def server(method, url, **kwargs):
            bodies.append(copy.deepcopy(kwargs['json']))
            return response({'error': 'response_format is unsupported'}, 400) if len(bodies) == 1 else chat('{"ok":true}')
        with patch.object(client.requests, 'request', side_effect=server):
            self.assertEqual(client.json_object('Return JSON'), {'ok': True})
        self.assertEqual(len(bodies), 2)
        self.assertIn('response_format', bodies[0])
        self.assertNotIn('response_format', bodies[1])

    def test_invalid_json_gets_one_correction_request(self):
        bodies = []
        def server(method, url, **kwargs):
            bodies.append(copy.deepcopy(kwargs['json']))
            return chat('an image prompt instead of JSON') if len(bodies) == 1 else chat('{"scenes":[]}')
        with patch.object(client.requests, 'request', side_effect=server):
            self.assertEqual(client.json_object('Narration: story'), {'scenes': []})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]['messages'][1], bodies[1]['messages'][1])
        self.assertEqual(bodies[1]['messages'][-2]['role'], 'assistant')

    def test_auth_error_not_retried_and_server_text_not_exposed(self):
        with patch.object(client.requests, 'request', return_value=response({'error': 'secret-test-key narration'}, 401)) as request:
            with self.assertRaisesRegex(client.TextServiceError, 'HTTP 401') as caught:
                client.json_object('private narration')
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('secret-test-key', str(caught.exception))
        self.assertNotIn('narration', str(caught.exception))

    def test_truncated_response_not_accepted_as_complete_plan(self):
        with patch.object(client.requests, 'request', return_value=chat('{"summary":"short"}', 'length')) as request:
            with self.assertRaisesRegex(client.TextServiceError, 'лимиту длины'):
                client.json_object('Story')
        self.assertEqual(request.call_count, 1)

    def test_content_blocks_supported(self):
        with patch.object(client.requests, 'request', return_value=chat([{'type': 'text', 'text': '{"name":"Имя"}'}])):
            self.assertEqual(client.json_object('Story'), {'name': 'Имя'})

    def test_parser_accepts_single_object_in_prose_and_rejects_ambiguous_or_invalid_data(self):
        self.assertEqual(client.parse_object('Вот ответ:\n{"name":"Иван {Петров}"}\nГотово.'), {'name': 'Иван {Петров}'})
        for bad in ('{"first":0} {"first":1}', '{"x":1,"x":2}', '{"x":NaN}',
                    '{"scenes":[{"first":0}],}', '[{"name":"test"}]'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                client.parse_object(bad)

    def test_models_legacy_catalogue_and_active_text_filter(self):
        with patch.object(client.requests, 'request', side_effect=[response({}, 404), response({'models': [
            {'id': 'image-model', 'media_type': 'image'}, {'id': 'disabled', 'media_type': 'text', 'active': False},
            {'id': 'text-model', 'media_type': 'text'}]})]) as request:
            self.assertEqual(client.available_models(), ['text-model'])
        self.assertTrue(request.call_args.args[1].endswith('/api/v6/models?media_type=text'))

    def test_failed_json_plan_preserves_existing_project_bytes_and_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / 'voice.wav')
            project.set_segments_from_transcript([{'text': 'История Ольги Корбут.', 'start': 0, 'end': 7}])
            portrait = Path(tmp) / 'portrait.jpg'; portrait.write_bytes(b'existing material')
            project.assign(0, portrait)
            before = project.project_file.read_bytes()
            with patch.object(client.requests, 'request', return_value=chat('not JSON')) as request:
                with self.assertRaisesRegex(client.TextServiceError, 'повторной попытки'):
                    plan_story(project.segments, log=lambda _: None)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(project.project_file.read_bytes(), before)
            self.assertEqual(Path(project.segments[0]['footage_path']), portrait)


if __name__ == '__main__':
    unittest.main()
