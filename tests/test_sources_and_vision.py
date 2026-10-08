import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import ai_client
import media
import planner
import visual_match
from project import Project, effective_source, source_matches


def checked(ok=True, score=.9):
    return {'compatible': ok, 'relevance_score': score, 'relevance_reason': 'test visual result',
            'visual_verified': True, 'relevance_basis': 'downloaded_visual_and_metadata'}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Project(Path(self.tmp.name) / 'voice.wav')
        self.p.set_segments_from_transcript([{'text': 'Ольга Корбут выступает на брусьях.', 'start': 0, 'end': 7}])
        self.s = self.p.segments[0]
        self.s.update(desired_kind='photo', search_query='Ольга Корбут брусья', query_en='Olga Korbut uneven bars', subject='Ольга Корбут')
        self.image = Path(self.tmp.name) / 'photo.png'
        Image.new('RGB', (120, 80), 'red').save(self.image)
        self.video = Path(self.tmp.name) / 'stock.mp4'
        self.video.touch()
        self.photo = {'id': 'photo', 'provider': 'web', 'media_kind': 'photo', 'photo_url': 'https://example.com/1.jpg'}
        self.stock = {'id': 'stock', 'provider': 'pexels', 'media_kind': 'video', 'video_url': 'https://example.com/1.mp4'}

    def test_global_stock_filters_saved_photos_and_never_falls_back_to_photo(self):
        self.p.settings['source_mode'] = 'stock'
        self.s['candidates'] = [self.photo]
        with patch.object(media, 'search', return_value=([], [])) as search, patch.object(media, 'download') as download:
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 0)
        download.assert_not_called()
        self.assertTrue(search.call_args_list)
        self.assertTrue(all(c.kwargs['kind'] == 'stock' for c in search.call_args_list))

    def test_global_photo_replaces_existing_stock_but_keeps_original_file(self):
        self.p.assign(0, self.video, self.stock)
        self.p.settings['source_mode'] = 'photo'
        self.s['candidates'] = [self.stock, self.photo]
        with patch.object(media, 'download', return_value=self.image) as download, \
             patch.object(media.visual_match, 'verify_file', return_value=checked()):
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 1)
        self.assertEqual(download.call_args.args[0]['id'], 'photo')
        self.assertEqual(self.s['media_kind'], 'photo')
        self.assertTrue(self.video.is_file())

    def test_individual_source_override_survives_save_and_global_photo_mode(self):
        self.p.settings['source_mode'] = 'photo'
        self.s['source_override'] = 'stock'
        self.s['candidates'] = [self.photo, self.stock]
        self.p.save()
        loaded = Project(self.p.audio_path)
        loaded.load()
        self.assertEqual(effective_source(loaded.settings, loaded.segments[0]), 'stock')
        with patch.object(media, 'download', return_value=self.video) as download, \
             patch.object(media.visual_match, 'verify_file', return_value=checked()):
            self.assertEqual(media.auto_pick(loaded, log=lambda _: None), 1)
        self.assertEqual(download.call_args.args[0]['provider'], 'pexels')

    def test_selected_youtube_not_substituted_when_external_access_disabled(self):
        self.p.settings['source_mode'] = 'youtube'
        with patch.object(media, 'search') as search:
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 0)
        search.assert_not_called()

    def test_wrong_visual_content_skipped_even_with_exact_title_and_high_score(self):
        self.s['candidates'] = [{**self.photo, 'id': 'wrong', 'title': 'Ольга Корбут', 'relevance_score': 1},
                                {**self.photo, 'id': 'right', 'photo_url': 'https://example.com/2.jpg', 'relevance_score': .5}]
        with patch.object(media, 'download', return_value=self.image), \
             patch.object(media.visual_match, 'verify_file', side_effect=[checked(False, .1), checked(True, .8)]) as verify:
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 1)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.s['selected_candidate_id'], 'right')
        self.assertTrue(self.s['visual_verified'])

    def test_ai_outage_stops_once_without_assigning_unchecked_result(self):
        self.s['candidates'] = [{**self.photo, 'id': str(i)} for i in range(8)]
        with patch.object(media, 'download', return_value=self.image) as download, \
             patch.object(media.visual_match, 'verify_file', side_effect=ai_client.TextServiceError('HTTP 429')) as verify, \
             patch.object(media, 'search') as search:
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 0)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(download.call_count, 1)
        search.assert_not_called()
        self.assertNotIn('footage_path', self.s)
        self.assertEqual(self.s['auto_pick_status'], 'verification_unavailable')

    def test_old_automatic_material_is_verified_without_downloading_again(self):
        self.p.assign(0, self.image, self.photo)
        self.s.update(review_status='automatically_selected', visual_verified=False)
        with patch.object(media.visual_match, 'verify_file', return_value=checked()) as verify, \
             patch.object(media, 'download') as download, patch.object(media, 'search') as search:
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None), 0)
        verify.assert_called_once()
        download.assert_not_called()
        search.assert_not_called()
        self.assertTrue(self.s['visual_verified'])

    def test_old_automatic_material_not_render_ready_during_ai_outage(self):
        from project import scene_ready
        self.p.assign(0, self.image, self.photo)
        self.s.update(review_status='automatically_selected', visual_verified=False)
        with patch.object(media.visual_match, 'verify_file', side_effect=ai_client.TextServiceError('offline')):
            media.auto_pick(self.p, log=lambda _: None)
        self.assertFalse(scene_ready(self.p.settings, self.s))
        self.assertTrue(self.image.is_file())

    def test_cancellation_during_visual_check_does_not_assign_file(self):
        stop = [False]
        self.s['candidates'] = [self.photo]
        def verify(*args):
            stop[0] = True
            return checked()
        with patch.object(media, 'download', return_value=self.image), \
             patch.object(media.visual_match, 'verify_file', side_effect=verify):
            self.assertEqual(media.auto_pick(self.p, log=lambda _: None, cancelled=lambda: stop[0]), 0)
        self.assertNotIn('footage_path', self.s)

    def test_stock_plan_enforces_source_even_if_model_proposes_photos(self):
        n = len(planner.units_from_words(planner.timed_words([self.s])))
        with patch.object(planner, 'llm_json', side_effect=[{'summary': 'Gymnast', 'entities': []},
                {'scenes': [{'first': 0, 'last': n-1, 'kind': 'photo', 'query': 'gymnast uneven bars', 'subject': 'gymnast'}]}]):
            result = planner.plan_story([self.s], {'source_mode': 'stock'}, log=lambda _: None)
        self.assertTrue(all(s['desired_kind'] == 'stock' for s in result['segments']))
        self.assertTrue(all(s['depiction'] == 'illustrative' for s in result['segments']))

    def test_different_actions_of_same_person_are_not_merged(self):
        base = {'start': 0, 'end': 4, 'text': 'Спортсменка тренируется.', 'subject': 'Ольга Корбут',
                'desired_kind': 'photo', 'search_query': 'Olga training', 'words': []}
        other = {**base, 'start': 4, 'end': 8, 'text': 'Получила медаль.', 'search_query': 'Olga medal'}
        self.assertEqual(len(planner.rebalance_scenes([base, other])), 2)

    def test_stock_query_refinement_uses_context_and_marks_illustration(self):
        with patch.object(planner, 'llm_json', return_value={'query': 'гимнастка брусья',
                'query_en': 'gymnast training uneven bars', 'must_match': ['uneven bars'],
                'required_entities': ['Ольга Корбут']}) as client:
            result = planner.refine_scene(self.s, 'stock', 'Ранее названа Ольга Корбут.')
        self.assertIn('source=stock', client.call_args.args[0])
        self.assertIn('Ранее названа Ольга', client.call_args.args[0])
        self.assertEqual(result['required_entities'], [])
        self.assertEqual(result['depiction'], 'illustrative')

    def test_individual_preference_is_carried_into_new_plan_without_assigned_file(self):
        from main import plan_and_pick
        self.s['source_override'] = 'stock'
        result = {'segments': [{'start': 0, 'end': 7, 'text': self.s['text'], 'desired_kind': 'photo'}],
                  'story': {'summary': 'Gymnast'}, 'name_titles': [], 'raw_transcript': [copy.deepcopy(self.s)]}
        with patch.object(planner, 'plan_story', return_value=result), patch.object(media, 'auto_pick', return_value=0):
            plan_and_pick(self.p, log=lambda _: None)
        self.assertEqual(self.p.segments[0]['source_override'], 'stock')
        self.assertEqual(self.p.segments[0]['desired_kind'], 'stock')
        self.assertTrue(self.p.segments[0]['source_query_dirty'])


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'actual.png'
        Image.new('RGB', (800, 600), 'green').save(self.path)
        self.scene = {'text': 'Гимнастка выступает на брусьях.', 'subject': 'Ольга Корбут', 'must_match': ['uneven bars']}
        self.c = {'id': 'one', 'provider': 'web', 'media_kind': 'photo', 'title': 'Olga Korbut', 'source_url': 'https://example.com'}

    def test_actual_photo_is_sent_with_narration_not_just_title(self):
        reply = {'ranked': [{'id': 'one', 'compatible': False, 'score': .1, 'reason': 'No uneven bars'}]}
        with patch.object(visual_match, 'json_object', return_value=reply) as client:
            result = visual_match.verify_file(self.scene, self.c, self.path)
        self.assertFalse(result['compatible'])
        self.assertIn('uneven bars', client.call_args.args[0])
        self.assertEqual(client.call_args.kwargs['images'][0][0], 'one')
        self.assertTrue(client.call_args.kwargs['images'][0][1].startswith('data:image/jpeg;base64,'))
        self.assertIn('Do not identify people by their faces', client.call_args.args[0])

    def test_unknown_duplicate_missing_or_nonboolean_visual_answers_rejected(self):
        valid = {'id': 'one', 'compatible': True, 'score': .9}
        for rows in ([], [{**valid, 'id': 'invented'}], [valid, valid], [{**valid, 'compatible': 'yes'}], [{**valid, 'score': float('nan')}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                visual_match.checked_rows({'ranked': rows}, [self.c])

    def test_preview_ranking_uses_images_and_marks_wrong_content(self):
        reply = {'ranked': [{'id': 'one', 'compatible': False, 'score': .1, 'reason': 'Wrong action'},
                             {'id': 'two', 'compatible': True, 'score': .9, 'reason': 'Right action'}]}
        candidates = [{**self.c, 'preview_image': 'https://example.com/a.png'},
                      {**self.c, 'id': 'two', 'preview_image': 'https://example.com/b.png'}]
        with patch.object(visual_match, 'preview_bytes', return_value=self.path.read_bytes()), \
             patch.object(visual_match, 'json_object', return_value=reply) as client:
            ranked = visual_match.rank_previews(self.scene, candidates)
        self.assertEqual(ranked[0]['id'], 'two')
        self.assertFalse(ranked[1]['compatible'])
        self.assertEqual(len(client.call_args.kwargs['images']), 2)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_video_samples_three_frames_from_used_eight_second_interval(self):
        video = Path(self.tmp.name) / 'clip.mp4'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=blue:s=160x90:r=25:d=2', '-c:v', 'libx264', '-threads', '2', str(video)], check=True)
        candidate = {**self.c, 'media_kind': 'video'}
        reply = {'ranked': [{'id': 'one', 'compatible': True, 'score': .8}]}
        with patch.object(visual_match, 'json_object', return_value=reply) as client:
            result = visual_match.verify_file(self.scene, candidate, video)
        images = client.call_args.kwargs['images']
        self.assertEqual(len(images), 3)
        self.assertEqual([label for label, _ in images], ['one frame 0', 'one frame 0.8', 'one frame 1.6'])
        self.assertTrue(all(url.startswith('data:image/jpeg;base64,') for _, url in images))
        self.assertTrue(result['visual_verified'])


if __name__ == '__main__':
    unittest.main()
