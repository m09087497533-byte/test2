import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media
from project import Project, validate_segments
from render import timeline
from subtitles import import_subtitle_file


class CoreTests(unittest.TestCase):
    def setUp(self):
        verifier = patch('media.visual_match.verify_file', return_value={
            'compatible': True, 'relevance_score': 0.9, 'relevance_reason': 'Verified test fixture',
            'visual_verified': True, 'relevance_basis': 'downloaded_visual_and_metadata'})
        verifier.start()
        self.addCleanup(verifier.stop)

    def test_pattern_persistence_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.set_segments_from_transcript([{'text': str(i), 'start': i * 2, 'end': i * 2 + 1} for i in range(4)])
            self.assertEqual([s['desired_kind'] for s in p.segments], ['auto'] * 4)
            file = Path(tmp) / 'photo.jpg'
            file.touch()
            p.assign(0, file)
            p.apply_pattern('Фото / стоки / YouTube')
            p.save()
            other = Project(p.audio_path)
            self.assertTrue(other.load())
            self.assertEqual(other.segments[3]['desired_kind'], 'youtube')
            self.assertEqual(other.segments[0]['footage_path'], str(file))
            p.project_file.write_text(json.dumps({'segments': [{'text': 'old', 'start': 0, 'end': 1,
                                                   'footage_path': str(file)}]}))
            self.assertTrue(other.load())
            self.assertEqual(other.segments[0]['media_kind'], 'photo')

    def test_pause_and_tail_timeline_no_drift(self):
        segs = [{'start': 0.7, 'end': 1.4}, {'start': 2.2, 'end': 2.8}, {'start': 4.001, 'end': 4.7}]
        items = timeline(segs, 5.03, 25)
        self.assertEqual(sum(s['frames'] for s in items), math.ceil(5.03 * 25))
        self.assertEqual(items[0]['timeline_start'], 0)
        self.assertEqual(items[0]['timeline_end'], 2.2)
        with self.assertRaises(ValueError):
            timeline(segs, 3, 25)

    def test_bad_timing_rejected(self):
        for segs in ([{'start': 2, 'end': 1}], [{'start': float('nan'), 'end': 2}],
                     [{'start': 0, 'end': 2}, {'start': 1, 'end': 3}]):
            with self.assertRaises(ValueError):
                validate_segments(segs)

    def test_vtt_short_timestamps_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.vtt'
            path.write_text('WEBVTT\n\n00:01.000 --> 00:02.000 align:start\n<i>Привет</i> мир\n\n00:04.000 --> 00:05.000\nДругая сцена\n')
            segs = import_subtitle_file(path)['segments']
            self.assertEqual(segs[0]['text'], 'Привет мир')
            self.assertEqual(segs[1]['start'], 4)

    def test_caption_window_and_no_match(self):
        events = [{'start': 0, 'text': 'welcome to the film'},
                  {'start': 12, 'text': 'historic railway steam locomotive'},
                  {'start': 15, 'text': 'train approaching the station'}]
        match = media.rank_caption_window(events, 'Поезд', 6, 'steam locomotive train')
        self.assertEqual(match['start'], 12)
        self.assertIsNone(media.rank_caption_window(events, 'ocean coral reef', 3))

    def test_external_permission_checked_before_download(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(media, 'run_ytdlp') as download:
            with self.assertRaises(ValueError):
                media.download({'provider': 'youtube', 'video_url': 'https://youtube.com/watch?v=abc',
                                'media_kind': 'video'}, tmp, 3)
            download.assert_not_called()

    def test_youtube_results_and_stock_failover(self):
        response = json.dumps({'entries': [{'id': 'abc', 'title': 'Steam train', 'duration': 20}]})
        with patch.object(media, 'run_ytdlp', return_value=response), patch.object(media, 'semantic_query', return_value='steam train'):
            candidates, warnings = media.search({'text': 'Поезд'}, 'youtube')
            self.assertEqual(candidates[0]['source_url'], 'https://www.youtube.com/watch?v=abc')
        with patch.object(media, 'semantic_query', return_value='train'), \
             patch.object(media.footage, 'PEXELS_API_KEY', 'test'), \
             patch.object(media.footage, 'PIXABAY_API_KEY', 'test'), \
             patch.object(media.footage, '_search_pexels', side_effect=TimeoutError), \
             patch.object(media.footage, '_search_pixabay', return_value=[{'id': 'test', 'provider': 'pixabay'}]):
            candidates, warnings = media.search({'text': 'train', 'search_query': 'train'}, 'stock')
            self.assertEqual(len(candidates), 1)
            self.assertTrue(any('Pexels' in w for w in warnings))

    def test_auto_tries_another_candidate_and_preserves_assigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.set_segments_from_transcript([{'text': 'one', 'start': 0, 'end': 1},
                                            {'text': 'two', 'start': 1, 'end': 2}])
            file = Path(tmp) / 'local.jpg'
            from PIL import Image, ImageDraw
            image = Image.new('RGB', (80, 80), 'white')
            ImageDraw.Draw(image).polygon([(0, 0), (79, 0), (0, 79)], fill='black')
            image.save(file)
            found = Path(tmp) / 'found.jpg'
            image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(found)
            p.assign(1, file)
            p.segments[0]['desired_kind'] = 'photo'
            p.segments[1]['desired_kind'] = 'photo'
            candidates = [{'id': 'bad', 'provider': 'web', 'media_kind': 'photo'},
                          {'id': 'good', 'provider': 'web', 'media_kind': 'photo'}]
            with patch.object(media, 'semantic_query', return_value='test subject'), \
                 patch.object(media, 'search', return_value=(candidates, [])), \
                 patch.object(media, 'download', side_effect=[RuntimeError('bad file'), found]) as download:
                self.assertEqual(media.auto_pick(p, log=lambda _: None), 1)
                self.assertEqual(download.call_count, 2)
            self.assertEqual(p.segments[0]['selected_candidate_id'], 'good')
            self.assertEqual(p.segments[1]['footage_source'], 'local')


if __name__ == '__main__':
    unittest.main()
