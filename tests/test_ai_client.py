import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_client as client
from planner import plan_story
from project import Project


def response(value, status=200):
    return Mock(ok=status < 400, status_code=status, text=json.dumps(value), json=Mock(return_value=value))


def chat(content, finish='stop'):
    return response({'choices': [{'message': {'content': content}, 'finish_reason': finish}]})


class TextClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'SCENEMIX_AI_PROVIDER': 'openai',
                                          'OPENAI_API_KEY': 'secret-test-key', 'OPENAI_MODEL': 'gpt-4.1-mini'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_uses_chat_endpoint_and_reads_fenced_content(self):
        with patch.object(client.requests, 'request', return_value=chat('```json\n{"summary":"Рассказ"}\n```')) as request:
            self.assertEqual(client.json_object('Read narration'), {'summary': 'Рассказ'})
        args, kwargs = request.call_args
        self.assertEqual(args, ('POST', 'https://api.openai.com/v1/chat/completions'))
        self.assertEqual(kwargs['json']['model'], 'gpt-4.1-mini')
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

    def test_direct_model_catalogue_and_bearer_auth(self):
        with patch.object(client.requests, 'request', return_value=response({'data': [{'id': 'gpt-4.1-mini'}]})) as request:
            self.assertEqual(client.available_models(), ['gpt-4.1-mini'])
        self.assertEqual(request.call_args.args[1], 'https://api.openai.com/v1/models')
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer secret-test-key')
        self.assertNotIn('X-API-Key', request.call_args.kwargs['headers'])

    def test_ollama_uses_local_api_without_sending_openai_key(self):
        with patch.dict(os.environ, {'SCENEMIX_AI_PROVIDER': 'ollama', 'OLLAMA_MODEL': 'gemma3:4b'}), \
             patch.object(client.requests, 'request', return_value=response({'message': {'content': '{"ok":true}'}, 'done_reason': 'stop'})) as request:
            self.assertEqual(client.json_object('test'), {'ok': True})
        self.assertEqual(request.call_args.args[1], 'http://localhost:11434/api/chat')
        self.assertNotIn('Authorization', request.call_args.kwargs['headers'])
        self.assertEqual(request.call_args.kwargs['json']['model'], 'gemma3:4b')
        self.assertEqual(request.call_args.kwargs['json']['options']['num_ctx'], 32768)

    def test_visual_input_keeps_label_and_image_in_user_message(self):
        with patch.object(client.requests, 'request', return_value=chat('{"ok":true}')) as request:
            client.json_object('Compare to narration', images=[('asset-1', 'data:image/jpeg;base64,AA==')])
        content = request.call_args.kwargs['json']['messages'][1]['content']
        self.assertEqual(content[1]['text'], 'asset-1')
        self.assertEqual(content[2]['image_url']['url'], 'data:image/jpeg;base64,AA==')

    def test_ollama_images_use_native_base64_format(self):
        with patch.dict(os.environ, {'SCENEMIX_AI_PROVIDER': 'ollama'}), \
             patch.object(client.requests, 'request', return_value=response({'message': {'content': '{"ok":true}'}})) as request:
            client.json_object('Compare', images=[('asset-1', 'data:image/jpeg;base64,AA==')])
        body = request.call_args.kwargs['json']
        self.assertEqual(body['messages'][1]['images'], ['AA=='])
        self.assertIn('asset-1', body['messages'][1]['content'])
        self.assertFalse(body['stream'])
        self.assertEqual(body['format'], 'json')

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
