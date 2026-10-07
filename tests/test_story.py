import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import media
from planner import mention_titles, timed_words, plan_story, save_plan, validate_groups
from project import Project
from search_policy import SearchPolicy, rank_candidates
from titles import write_name_ass


class StoryTests(unittest.TestCase):
    def test_name_uses_word_time_not_scene_start_and_not_pronoun(self):
        words = [
            {'text': 'Здесь', 'start': 10, 'end': 10.3, 'timing_quality': 'word'},
            {'text': 'Ольга', 'start': 12.25, 'end': 12.7, 'timing_quality': 'word'},
            {'text': 'Корбут.', 'start': 12.7, 'end': 13, 'timing_quality': 'word'},
            {'text': 'Она', 'start': 14, 'end': 14.4, 'timing_quality': 'word'},
            {'text': 'победила.', 'start': 14.4, 'end': 15, 'timing_quality': 'word'}]
        titles = mention_titles(words, [{'name': 'Ольга Корбут', 'aliases': ['Ольга', 'она']}])
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]['start'], 12.25)
        self.assertEqual(titles[0]['timing_quality'], 'word')

    def test_srt_word_timing_explicitly_estimated(self):
        words = timed_words([{'start': 0, 'end': 4, 'text': 'Ольга Корбут выигрывает золото'}])
        titles = mention_titles(words, [{'name': 'Ольга Корбут'}])
        self.assertEqual(titles[0]['timing_quality'], 'estimated')

    def test_planner_chooses_same_kind_twice_and_preserves_people(self):
        transcript = [{'text': 'Ольга Корбут родилась в городе. Она тренировалась с детства. '
                                'Ольга Корбут выступила на Олимпиаде.', 'start': 0, 'end': 18}]
        from planner import units_from_words
        n = len(units_from_words(timed_words(transcript)))
        overview = {'summary': 'История Ольги Корбут', 'era': '1972', 'entities': [{'name': 'Ольга Корбут'}]}
        groups = {'scenes': [{'first': 0, 'last': n - 1, 'kind': 'photo', 'subject': 'Ольга Корбут',
                             'query': 'Ольга Корбут портрет', 'reason': 'Человек', 'effect': 'slide_up'}]}
        with patch('planner.llm_json', side_effect=[overview, groups]):
            result = plan_story(transcript, log=lambda _: None)
        self.assertEqual([s['desired_kind'] for s in result['segments']], ['photo', 'photo'])
        self.assertTrue(all(s['subject'] == 'Ольга Корбут' for s in result['segments']))
        self.assertEqual(' '.join(s['text'] for s in result['segments']), transcript[0]['text'])
        self.assertTrue(result['name_titles'])

    def test_bad_llm_coverage_rejected(self):
        with self.assertRaises(ValueError):
            validate_groups([{'first': 1, 'last': 3, 'kind': 'photo', 'query': 'test'}], 4)
        with self.assertRaises(ValueError):
            validate_groups([{'first': 0, 'last': 1, 'kind': 'photo', 'query': 'test'}], 4)

    def test_replan_backup_and_roundtrip_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.set_segments_from_transcript([{'text': 'old', 'start': 0, 'end': 2}])
            p.save()
            previous = p.project_file.read_bytes()
            result = {'segments': [{'text': 'new', 'start': 0, 'end': 8, 'index': 0, 'desired_kind': 'youtube'}],
                      'story': {'summary': 'new'}, 'name_titles': [{'name': 'Имя', 'start': 2, 'end': 4}],
                      'raw_transcript': [{'text': 'old', 'start': 0, 'end': 2}]}
            save_plan(p, result)
            backup = next(p.project_dir.glob('project.before-plan.*.json'))
            self.assertEqual(backup.read_bytes(), previous)
            q = Project(p.audio_path); q.load()
            self.assertEqual(q.name_titles, result['name_titles'])
            self.assertEqual(q.raw_transcript, result['raw_transcript'])

    def test_rate_limit_halts_provider_across_new_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = urllib.error.HTTPError('https://api.pexels.com', 429, 'limit', {'Retry-After': '120'}, None)
            function = unittest.mock.Mock(side_effect=error)
            policy = SearchPolicy(tmp)
            with self.assertRaisesRegex(RuntimeError, '429'):
                policy.call('Pexels', 'gymnast', function)
            fresh = SearchPolicy(tmp)
            with self.assertRaisesRegex(RuntimeError, 'лимит запросов'):
                fresh.call('Pexels', 'different query', function)
            self.assertEqual(function.call_count, 1)

    def test_search_cache_avoids_extra_api_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            function = unittest.mock.Mock(return_value=[{'id': 'one'}])
            p = SearchPolicy(tmp)
            p.call('Pexels', 'gymnast', function)
            fresh = SearchPolicy(tmp)
            self.assertEqual(fresh.call('Pexels', 'gymnast', function), [{'id': 'one'}])
            self.assertEqual(function.call_count, 1)

    def test_unrelated_metadata_is_ranked_below_person(self):
        c = [{'id': 'city', 'provider': 'pexels', 'title': 'Night street'},
             {'id': 'person', 'provider': 'web', 'title': 'Ольга Корбут 1972'}]
        with patch('planner.llm_json', return_value={'ranked': [
            {'id': 'city', 'score': 0.05, 'compatible': False, 'reason': 'не тот сюжет'},
            {'id': 'person', 'score': 0.95, 'reason': 'тот человек'}]}):
            ranked = rank_candidates({'subject': 'Ольга Корбут'}, c, {})
        self.assertEqual(ranked[0]['id'], 'person')
        self.assertLess(ranked[1]['relevance_score'], 0.8)
        self.assertFalse(ranked[1]['compatible'])

    def test_youtube_no_subtitle_match_never_uses_start_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.set_segments_from_transcript([{'text': 'Ольга Корбут', 'start': 0, 'end': 7}])
            p.segments[0]['desired_kind'] = 'youtube'
            p.settings['allow_external'] = True
            c = {'id': 'candidate', 'provider': 'youtube', 'source_url': 'https://youtube.com/watch?v=a'}
            with patch.object(media, 'search', return_value=([c], [])), \
                 patch.object(media, 'suggest_start', return_value=None), patch.object(media, 'download') as download:
                media.auto_pick(p, log=lambda _: None)
                download.assert_not_called()
            self.assertFalse(p.segments[0].get('footage_path'))

    def test_external_download_request_never_exceeds_eight_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            def downloader(args, timeout=90):
                seen.append(args)
                template = args[args.index('--output') + 1]
                Path(template.replace('%(ext)s', 'mp4')).write_bytes(b'placeholder')
                return ''
            with patch.object(media, 'run_ytdlp', side_effect=downloader), \
                 patch.object(media.footage, '_verify_video', return_value=True), \
                 patch.object(media.subprocess, 'run'):
                media.download({'provider': 'youtube', 'media_kind': 'video',
                                'video_url': 'https://youtube.com/watch?v=a'}, tmp, 19, 12.5, True)
            section = seen[0][seen[0].index('--download-sections') + 1]
            self.assertEqual(section, '*12.500-20.500')

    def test_ass_absolute_times_and_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'name.ass'
            write_name_ass([{'name': 'Ольга {\\an8} Корбут', 'start': 12.25, 'end': 16}], path, 1280, 720, 20)
            text = path.read_text()
            self.assertIn('0:00:12.25,0:00:16.00', text)
            self.assertNotIn('{\\an8}', text)
            self.assertIn('PrimaryColour', text)


if __name__ == '__main__':
    unittest.main()
