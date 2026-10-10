import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import media
from main import plan_and_pick
from project import Project
from search_policy import SearchPolicy


class AutomaticSelectionTests(unittest.TestCase):
    def setUp(self):
        verifier = patch('media.visual_match.verify_file', return_value={
            'compatible': True, 'relevance_score': 0.9, 'relevance_reason': 'Verified test fixture',
            'visual_verified': True, 'relevance_basis': 'downloaded_visual_and_metadata'})
        verifier.start()
        self.addCleanup(verifier.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / 'voice.wav')
        self.project.set_segments_from_transcript([{'text': 'Ольга Корбут выступает.', 'start': 0, 'end': 7}])
        self.scene = self.project.segments[0]
        self.scene.update(desired_kind='photo', strict_relevance=True,
                          search_query='Ольга Корбут Олимпиада 1972', query_en='Olga Korbut Olympics 1972', subject='Ольга Корбут')
        self.image = Path(self.tmp.name) / 'found.png'
        Image.new('RGB', (160, 90), '#ab3549').save(self.image)

    def candidate(self, identity='photo', score=0.55, **extra):
        return {'id': identity, 'provider': 'web', 'media_kind': 'photo', 'relevance_score': score,
                'photo_url': f'https://example.com/{identity}.jpg', **extra}

    def test_best_saved_result_below_former_threshold_is_assigned_without_search_or_choice(self):
        self.scene['candidates'] = [self.candidate('less', 0.2), self.candidate('best', 0.65), self.candidate('middle', 0.4)]
        progress = []
        with patch.object(media, 'search') as search, patch.object(media, 'download', return_value=self.image) as download:
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None, progress=lambda *x: progress.append(x)), 1)
        search.assert_not_called()
        self.assertEqual(download.call_args.args[0]['id'], 'best')
        self.assertEqual(self.scene['selected_candidate_id'], 'best')
        self.assertEqual(self.scene['auto_pick_status'], 'assigned')
        self.assertEqual(progress[-1], (1, 1))

    def test_sixth_link_is_tried_when_first_five_are_broken(self):
        candidates = [self.candidate(str(i), 1 - i * 0.1) for i in range(6)]
        with patch.object(media, 'search', return_value=(candidates, [])), \
             patch.object(media, 'download', side_effect=[RuntimeError('dead link')] * 5 + [self.image]) as download:
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(download.call_count, 6)
        self.assertEqual(self.scene['selected_candidate_id'], '5')

    def test_empty_primary_query_automatically_tries_english_query(self):
        with patch.object(media, 'search', side_effect=[([], []), ([self.candidate()], [])]) as search, \
             patch.object(media, 'download', return_value=self.image):
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(search.call_args_list[0].args[0]['search_query'], 'Ольга Корбут Олимпиада 1972')
        self.assertEqual(search.call_args_list[1].args[0]['search_query'], 'Olga Korbut Olympics 1972')
        self.assertEqual(search.call_args.kwargs['limit'], 18)

    def test_unconfirmed_video_automatically_falls_back_to_related_photo(self):
        self.scene['desired_kind'] = 'youtube'
        self.project.settings['allow_external'] = True
        video = {'id': 'yt', 'provider': 'youtube', 'media_kind': 'video', 'relevance_score': 0.9,
                 'video_url': 'https://youtube.com/watch?v=a', 'source_url': 'https://youtube.com/watch?v=a'}
        def search(seg, **kwargs):
            return ([video] if kwargs['kind'] == 'youtube' else [self.candidate()], [])
        with patch.object(media, 'search', side_effect=search), patch.object(media, 'suggest_start', return_value=None), \
             patch.object(media, 'download', return_value=self.image) as download:
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[0]['media_kind'], 'photo')
        self.assertEqual(self.scene['planned_kind'], 'youtube')
        self.assertEqual(self.scene['desired_kind'], 'photo')

    def test_stock_service_failure_falls_back_to_photo(self):
        self.scene['desired_kind'] = 'stock'
        def search(seg, **kwargs):
            if kwargs['kind'] == 'stock':
                raise RuntimeError('provider cooldown')
            return [self.candidate()], []
        with patch.object(media, 'search', side_effect=search), patch.object(media, 'download', return_value=self.image):
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(self.scene['media_kind'], 'photo')

    def test_explicitly_wrong_person_not_selected_even_with_high_score(self):
        self.scene['candidates'] = [self.candidate('wrong', 0.99, compatible=False), self.candidate('related', 0.4)]
        with patch.object(media, 'download', return_value=self.image) as download:
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(download.call_args.args[0]['id'], 'related')

    def test_explicit_rejection_kept_when_later_fallback_ranking_is_less_certain(self):
        self.scene['candidates'] = [self.candidate('wrong', 0.99, compatible=False)]
        with patch.object(media, 'search', return_value=([self.candidate('wrong', 0.99, compatible=True),
                                                        self.candidate('related', 0.4)], [])), \
             patch.object(media, 'download', return_value=self.image) as download:
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[0]['id'], 'related')

    def test_two_different_photos_on_same_article_are_not_treated_as_duplicates(self):
        second = Path(self.tmp.name) / 'second.png'
        Image.new('RGB', (90, 160), '#24a840').save(second)
        self.project.set_segments_from_transcript([{'text': 'one', 'start': 0, 'end': 6}, {'text': 'two', 'start': 6, 'end': 12}])
        for i, scene in enumerate(self.project.segments):
            scene.update(desired_kind='photo', search_query=str(i))
        with patch.object(media, 'search', side_effect=[([self.candidate('first', source_url='https://example.com/article')], []),
                                                       ([self.candidate('second', source_url='https://example.com/article')], [])]), \
             patch.object(media, 'download', side_effect=[self.image, second]):
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 2)
        self.assertTrue(all(s.get('footage_path') for s in self.project.segments))

    def test_download_cancellation_does_not_assign_or_continue_searching(self):
        stop = threading.Event()
        def download(*args):
            stop.set()
            return self.image
        with patch.object(media, 'search', return_value=([self.candidate()], [])) as search, \
             patch.object(media, 'download', side_effect=download):
            self.assertEqual(media.auto_pick(self.project, log=lambda _: None, cancelled=stop.is_set), 0)
        self.assertEqual(search.call_count, 1)
        self.assertNotIn('footage_path', self.scene)

    def test_unavailable_sources_finish_bounded_search_without_requesting_manual_choices(self):
        logs = []
        with patch.object(media, 'search', return_value=([], [])) as search:
            self.assertEqual(media.auto_pick(self.project, log=logs.append), 0)
        self.assertEqual(search.call_count, 3)
        self.assertEqual(self.scene['auto_pick_status'], 'temporarily_unavailable')
        self.assertFalse(any('требуют выбора' in line or 'укажите вручную' in line for line in logs))

    def test_basic_search_never_calls_text_model_even_with_legacy_strict_flag(self):
        candidates = [{'id': 'unrelated', 'provider': 'web', 'title': 'Night city', 'photo_url': 'https://example.com/city.jpg'},
                      {'id': 'person', 'provider': 'web', 'title': 'Ольга Корбут 1972', 'photo_url': 'https://example.com/person.jpg'}]
        policy = SearchPolicy(self.project.project_dir)
        with patch.object(media.photo, '_search_web_images', return_value=(candidates, 0)), \
             patch.object(media, 'rank_candidates', side_effect=RuntimeError('API offline')) as rank:
            first, warnings = media.search(self.scene, policy=policy)
            second, _ = media.search(self.scene, policy=policy)
        self.assertEqual(rank.call_count, 0)
        self.assertEqual(first[0]['id'], 'person')
        self.assertEqual(second[0]['id'], 'person')
        self.assertFalse(warnings)
        self.assertEqual(first[0]['relevance_basis'], 'context_and_source_keywords')

    def test_bigger_search_pool_not_hidden_by_old_six_result_cache(self):
        policy = SearchPolicy(self.project.project_dir)
        self.scene['strict_relevance'] = False
        def search(query, limit):
            return [self.candidate(str(i)) for i in range(limit)], 0
        with patch.object(media.photo, '_search_web_images', side_effect=search) as web:
            six, _ = media.search(self.scene, limit=6, policy=policy)
            eighteen, _ = media.search(self.scene, limit=18, policy=policy)
        self.assertEqual(len(six), 6)
        self.assertEqual(len(eighteen), 18)
        self.assertEqual(web.call_count, 2)

    def test_one_click_plans_old_project_and_preserves_assignments_with_same_scene_boundaries(self):
        self.project.assign(0, self.image)
        self.project.set_segments_from_transcript([{'text': 'Ольга Корбут выступает.', 'start': 0, 'end': 7},
                                                  {'text': 'Другой эпизод.', 'start': 7, 'end': 14}])
        scenes = [{'text': s['text'], 'start': s['start'], 'end': s['end'], 'desired_kind': 'photo'} for s in self.project.segments]
        result = {'segments': scenes, 'story': {'summary': 'story'}, 'name_titles': [], 'raw_transcript': []}
        with patch('main.planner.plan_story', return_value=result) as plan, patch.object(media, 'auto_pick', return_value=1):
            self.assertEqual(plan_and_pick(self.project, log=lambda _: None), 1)
        plan.assert_called_once()
        self.assertEqual(self.project.segments[0]['footage_path'], str(self.image))
        self.assertTrue(list(self.project.project_dir.glob('project.before-plan.*.json')))

    def test_real_http_download_skips_dead_link_and_assigns_verified_photo(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        content = io.BytesIO()
        Image.new('RGB', (160, 90), '#4c8426').save(content, format='PNG')
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                requests.append(handler.path)
                if handler.path == '/dead.jpg':
                    handler.send_error(404)
                    return
                handler.send_response(200)
                handler.send_header('Content-Type', 'image/png')
                handler.end_headers()
                handler.wfile.write(content.getvalue())
            def log_message(*args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        self.scene['candidates'] = [self.candidate('dead', 0.7, photo_url=base + '/dead.jpg'),
                                    self.candidate('live', 0.6, photo_url=base + '/live.png')]
        try:
            with patch.object(media.photo, '_contains_watermark_text', return_value=None):
                self.assertEqual(media.auto_pick(self.project, log=lambda _: None), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(requests, ['/dead.jpg', '/live.png'])
        self.assertEqual(self.scene['selected_candidate_id'], 'live')
        with Image.open(self.scene['footage_path']) as image:
            self.assertEqual(image.size, (160, 90))


if __name__ == '__main__':
    unittest.main()
