import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_search import people, rank_results, scene_query
from planner import plan_story, refine_scene
from project import Project, validate_segments
import media


class ContextSearchTests(unittest.TestCase):
    def test_pronoun_keeps_person_year_and_specific_new_action(self):
        first = scene_query('Ольга Корбут выступила на Олимпиаде 1972 года.')
        following = scene_query('Она выполнила упражнение на брусьях.', state=first['_state'])
        self.assertIn('Ольга Корбут', following['search_query'])
        self.assertIn('1972', following['search_query'])
        self.assertIn('брусья', following['search_query'])
        self.assertNotIn('Олимпиада', following['search_query'])
        self.assertEqual(following['required_entities'], ['Ольга Корбут'])

    def test_changing_person_does_not_inherit_old_action_or_year(self):
        first = scene_query('Ольга Корбут выступила на брусьях в 1972 году.')
        other = scene_query('Иван Иванов — известный художник.', state=first['_state'])
        self.assertNotIn('брусья', other['search_query'])
        self.assertNotIn('1972', other['search_query'])
        self.assertEqual(other['_state']['concepts'], [])
        following = scene_query('Он написал книгу.', state=other['_state'])
        self.assertIn('Иван Иванов', following['search_query'])
        self.assertNotIn('Корбут', following['search_query'])

    def test_new_event_without_pronoun_does_not_inherit_person(self):
        first = scene_query('Ольга Корбут выступила на брусьях.')
        other = scene_query('Затем произошло землетрясение в Японии.', state=first['_state'])
        self.assertNotIn('Корбут', other['search_query'])
        self.assertIn('землетрясение', other['search_query'])
        self.assertIn('Япония', other['search_query'])

    def test_unknown_action_uses_its_words_rather_than_previous_action(self):
        first = scene_query('Ольга Корбут выступила на брусьях.')
        other = scene_query('Телевизор перестал работать.', state=first['_state'])
        self.assertIn('телевизор', other['search_query'])
        self.assertNotIn('брусья', other['search_query'])

    def test_stock_search_illustrates_action_without_claiming_identity(self):
        result = refine_scene({'text': 'Она выступила на брусьях.'}, 'stock',
                              'Ольга Корбут участвовала в Олимпиаде 1972 года.')
        self.assertIn('uneven bars', result['search_query'])
        self.assertNotIn('Корбут', result['search_query'])
        self.assertEqual(result['required_entities'], [])
        self.assertEqual(result['depiction'], 'illustrative')

    def test_event_query_preserves_specific_topic(self):
        result = scene_query('В 1991 году произошёл распад СССР.', kind='stock')
        self.assertIn('Soviet Union', result['search_query'])
        self.assertIn('collapse', result['search_query'])

    def test_speech_is_not_sport_and_staircase_is_not_forest(self):
        speech = scene_query('Иван Иванов выступил с речью.', kind='stock')
        self.assertIn('public speech', speech['search_query'])
        self.assertNotIn('sports', speech['search_query'])
        stairs = scene_query('Лестница стоит в комнате.')
        self.assertNotIn('лес', stairs['must_match'])
        training = scene_query('athlete training', kind='stock')
        self.assertNotIn('railway', training['search_query'])

    def test_common_name_forms_preserve_spoken_alias(self):
        entity = people('Вспомним Ольгу Корбут и Марию Шарапову.')[1]
        self.assertEqual(entity['name'], 'Мария Шарапова')
        self.assertIn('Марию Шарапову', entity['aliases'])

    def test_named_results_rank_english_and_russian_matches_over_wrong_person(self):
        scene = scene_query('Ольга Корбут выступила на брусьях в 1972 году.')
        candidates = [
            {'id': 'city', 'title': 'Night city', 'source_url': 'https://example.com/night'},
            {'id': 'wrong', 'title': 'Olga Petrova Olympics 1972'},
            {'id': 'english', 'title': 'Olga Korbut uneven bars 1972'},
            {'id': 'russian', 'title': 'Ольга Корбут: брусья 1972'},
        ]
        original = copy.deepcopy(candidates)
        ranked = rank_results(scene, candidates)
        self.assertEqual({c['id'] for c in ranked[:2]}, {'english', 'russian'})
        self.assertTrue(all(c['compatible'] for c in ranked[:2]))
        self.assertTrue(all(not c['compatible'] for c in ranked[2:]))
        self.assertEqual(candidates, original)
        self.assertTrue(all(c['relevance_basis'] == 'context_and_source_keywords' for c in ranked))

    def test_query_parameter_is_not_evidence_of_person(self):
        scene = scene_query('Ольга Корбут выступила на брусьях.')
        result = rank_results(scene, [{'title': 'Night city',
            'source_url': 'https://example.com/search?q=Olga%20Korbut'}])[0]
        self.assertFalse(result['compatible'])

    def test_previously_rejected_candidate_stays_rejected(self):
        scene = scene_query('Ольга Корбут выступила на брусьях.')
        self.assertFalse(rank_results(scene, [{'title': 'Ольга Корбут брусья', 'compatible': False}])[0]['compatible'])

    def test_basic_planning_makes_no_network_or_model_calls_and_keeps_all_text(self):
        transcript = [
            {'text': 'Ольга Корбут выступила на Олимпиаде 1972 года.', 'start': 0, 'end': 8},
            {'text': 'Она выполнила упражнение на брусьях.', 'start': 8, 'end': 16},
            {'text': 'В Японии произошло сильное землетрясение.', 'start': 18, 'end': 26},
            {'text': 'Ванга сделала предсказание.', 'start': 26, 'end': 34},
        ]
        with patch('planner.llm_json', side_effect=AssertionError('No AI')) as ai, \
             patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            result = plan_story(transcript, log=lambda _: None)
        ai.assert_not_called()
        validate_segments(result['segments'])
        self.assertEqual(' '.join(s['text'] for s in result['segments']), ' '.join(s['text'] for s in transcript))
        self.assertEqual(result['raw_transcript'], transcript)
        self.assertEqual(result['story']['analysis_basis'], 'context_rules')
        before = [s for s in result['segments'] if s['end'] <= 18]
        self.assertTrue(all('Ванга' not in s['search_query'] for s in before))
        self.assertTrue(any('брусья' in s['search_query'] for s in before))
        self.assertTrue(any(s['desired_kind'] == 'stock' and 'earthquake' in s['search_query'] for s in result['segments']))
        self.assertEqual(result['name_titles'][0]['name'], 'Ольга Корбут')
        self.assertEqual(result['name_titles'][0]['start'], 0)

    def test_stock_only_long_story_keeps_every_word_and_video_cap(self):
        transcript = [{'text': 'Поезд едет по железной дороге.', 'start': i * 8, 'end': (i + 1) * 8} for i in range(24)]
        result = plan_story(transcript, {'source_mode': 'stock'}, log=lambda _: None)
        scenes = result['segments']
        self.assertEqual(' '.join(s['text'] for s in scenes), ' '.join(s['text'] for s in transcript))
        self.assertTrue(all(s['desired_kind'] == 'stock' and s['end'] - s['start'] <= 8.001 for s in scenes))
        self.assertTrue(all(not s['required_entities'] for s in scenes))

    def test_saved_ai_project_migrates_to_basic_without_changing_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / 'voice.wav')
            project.set_segments_from_transcript([{'text': 'Ольга Корбут.', 'start': 0, 'end': 6}])
            project.segments[0]['footage_path'] = str(Path(tmp) / 'ready.png')
            project.settings.update(analysis_mode='ai', visual_verification=True)
            project.save()
            loaded = Project(project.audio_path)
            loaded.load()
            self.assertEqual(loaded.settings['analysis_mode'], 'basic')
            self.assertFalse(loaded.settings['visual_verification'])
            self.assertEqual(loaded.segments[0]['footage_path'], project.segments[0]['footage_path'])

    def test_cached_results_rechecked_without_ai_before_automatic_assignment(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / 'voice.wav')
            project.set_segments_from_transcript([{'text': 'Ольга Корбут выступила на брусьях.', 'start': 0, 'end': 6}])
            scene = project.segments[0]
            metadata = scene_query(scene['text'])
            metadata.pop('_state')
            scene.update(metadata, desired_kind='photo', candidates=[
                {'id': 'wrong', 'title': 'Night city', 'provider': 'web', 'media_kind': 'photo',
                 'photo_url': 'https://example.com/wrong.jpg', 'relevance_score': 0.99},
                {'id': 'right', 'title': 'Olga Korbut uneven bars', 'provider': 'web', 'media_kind': 'photo',
                 'photo_url': 'https://example.com/right.jpg', 'relevance_score': 0.2},
            ])
            image = Path(tmp) / 'found.png'
            Image.new('RGB', (160, 90), 'green').save(image)
            with patch.object(media, 'search') as search, patch.object(media, 'download', return_value=image) as download, \
                 patch.object(media.visual_match, 'verify_file', side_effect=AssertionError('No vision model')):
                self.assertEqual(media.auto_pick(project, log=lambda _: None), 1)
            search.assert_not_called()
            self.assertEqual(download.call_args.args[0]['id'], 'right')
            self.assertEqual(scene['selected_candidate_id'], 'right')


if __name__ == '__main__':
    unittest.main()
