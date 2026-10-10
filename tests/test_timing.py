"""Overlapping recognizer/SRT timings must not stop automatic montage."""
import copy
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner import mention_titles, plan_story, timed_words
from project import Project, normalize_segment_timing, validate_segments
from render import build_video, probe, timeline
from subtitles import import_subtitle_file
from transcribe import split_into_scenes


class TimingTests(unittest.TestCase):
    def test_overlap_at_scene_1062_preserves_starts_text_files_and_no_drift(self):
        original = [{'start': i * 6.0, 'end': i * 6.0 + 6, 'text': str(i),
                     'footage_path': f'/media/{i}.jpg', 'source_start': 1.2} for i in range(1100)]
        original[1060]['end'] += 0.24
        before = copy.deepcopy(original)
        fixed, count = normalize_segment_timing(original)
        self.assertEqual(count, 1)
        self.assertEqual(original, before)
        self.assertEqual(fixed[1060]['end'], original[1061]['start'])
        for old, new in zip(original, fixed):
            for key in ('start', 'text', 'footage_path', 'source_start'):
                self.assertEqual(old[key], new[key])
        items = timeline(original, 6600.03, 25)
        self.assertEqual(sum(s['frames'] for s in items), math.ceil(6600.03 * 25))
        self.assertEqual(items[1061]['timeline_start'], original[1061]['start'])
        self.assertEqual(normalize_segment_timing(fixed), (fixed, 0))

    def test_gaps_and_last_end_unchanged(self):
        original = [{'start': 1, 'end': 4}, {'start': 3, 'end': 5}, {'start': 8, 'end': 9}]
        fixed, _ = normalize_segment_timing(original)
        self.assertEqual([(s['start'], s['end']) for s in fixed], [(1, 3), (3, 5), (8, 9)])

    def test_equal_starts_keep_every_scene_and_assignment(self):
        original = [{'start': 0, 'end': 4, 'text': 'first', 'footage_path': '/first.jpg'},
                    {'start': 0, 'end': 6, 'text': 'second', 'footage_path': '/second.jpg'},
                    {'start': 4, 'end': 7, 'text': 'third', 'footage_path': '/third.jpg'}]
        fixed, _ = normalize_segment_timing(original)
        validate_segments(fixed)
        self.assertEqual(len(fixed), 3)
        self.assertEqual([(s['start'], s['end']) for s in fixed], [(0, 2), (2, 4), (4, 7)])
        self.assertEqual([s['footage_path'] for s in fixed], [s['footage_path'] for s in original])

    def test_invalid_data_is_not_silently_retimed(self):
        for original in ([{'start': 2, 'end': 1}], [{'start': -1, 'end': 2}],
                         [{'start': float('nan'), 'end': 2}],
                         [{'start': 4, 'end': 8}, {'start': 2, 'end': 6}]):
            with self.assertRaises(ValueError):
                normalize_segment_timing(original)

    def test_old_project_auto_repair_backup_and_preserve_name_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.segments = [{'start': 0, 'end': 4.2, 'text': 'first', 'footage_path': '/first.jpg'},
                          {'start': 4, 'end': 8, 'text': 'second', 'footage_path': '/second.jpg'}]
            p.raw_transcript = copy.deepcopy(p.segments)
            p.name_titles = [{'name': 'Ольга Корбут', 'start': 4.25, 'end': 8}]
            p.save()
            before = p.project_file.read_bytes()
            loaded = Project(p.audio_path)
            self.assertTrue(loaded.load())
            self.assertEqual(loaded.timing_repair_count, 2)
            self.assertEqual(loaded.segments[0]['end'], 4)
            self.assertEqual(loaded.segments[1]['footage_path'], '/second.jpg')
            self.assertEqual(loaded.name_titles, p.name_titles)
            backups = list(p.project_dir.glob('project.before-timing.*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            self.assertTrue(Project(p.audio_path).load())
            self.assertEqual(len(list(p.project_dir.glob('project.before-timing.*.json'))), 1)

    def test_failed_load_does_not_overwrite_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Project(Path(tmp) / 'voice.wav')
            p.segments = [{'start': 5, 'end': 8, 'text': 'one'}, {'start': 1, 'end': 3, 'text': 'two'}]
            p.save()
            before = p.project_file.read_bytes()
            with self.assertRaises(ValueError):
                Project(p.audio_path).load()
            self.assertEqual(p.project_file.read_bytes(), before)
            self.assertFalse(list(p.project_dir.glob('project.before-timing.*.json')))

    def test_import_overlap_before_subdividing_long_cues(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'overlap.srt'
            path.write_text('1\n00:00:00,000 --> 00:00:20,000\none two three four five six\n\n'
                            '2\n00:00:05,000 --> 00:00:10,000\nseven eight\n\n'
                            '3\n00:00:12,000 --> 00:00:15,000\nnine ten\n')
            result = import_subtitle_file(path, max_duration=3)
            validate_segments(result['segments'])
            self.assertEqual(result['text'], 'one two three four five six seven eight nine ten')
            self.assertEqual(result['timing_repairs'], 1)
            self.assertEqual(result['segments'][-1]['end'], 15)

    def test_overlapping_word_ends_keep_exact_name_start(self):
        words = [{'text': 'Здесь', 'start': 10, 'end': 12.3},
                 {'text': 'Ольга', 'start': 12.25, 'end': 12.9},
                 {'text': 'Корбут.', 'start': 12.7, 'end': 13}]
        fixed = timed_words([{'text': 'Здесь Ольга Корбут.', 'start': 10, 'end': 13, 'words': words}])
        validate_segments(fixed)
        self.assertEqual([w['start'] for w in fixed], [10, 12.25, 12.7])
        titles = mention_titles(fixed, [{'name': 'Ольга Корбут'}])
        self.assertEqual(titles[0]['start'], 12.25)
        self.assertEqual(titles[0]['timing_quality'], 'word')

    def test_zero_length_and_same_start_words_are_not_lost(self):
        original = [{'text': 'Ольга', 'start': 0, 'end': 0},
                    {'text': 'Корбут', 'start': 0, 'end': 1},
                    {'text': 'победила.', 'start': 1, 'end': 2}]
        seg = {'text': 'Ольга Корбут победила.', 'start': 0, 'end': 2, 'words': original}
        words = timed_words([seg])
        validate_segments(words)
        self.assertEqual([w['text'] for w in words], [w['text'] for w in original])
        self.assertEqual(words[1]['timing_quality'], 'adjusted')
        split = split_into_scenes([seg], max_duration=1, min_duration=0.5)
        validate_segments(split)
        self.assertEqual(' '.join(s['text'] for s in split), seg['text'])

    def test_word_timing_defects_checked_before_llm_requests(self):
        seg = {'text': 'one two', 'start': 0, 'end': 10,
               'words': [{'text': 'one', 'start': 5, 'end': 6}, {'text': 'two', 'start': 1, 'end': 2}]}
        with patch('planner.llm_json') as client, self.assertRaises(ValueError):
            plan_story([seg], log=lambda _: None)
        client.assert_not_called()

    def test_equal_cues_with_conflicting_words_use_estimates_without_text_loss(self):
        original = [{'text': 'one two', 'start': 0, 'end': 4, 'timing_quality': 'word',
                     'words': [{'text': 'one', 'start': 0, 'end': 2}, {'text': 'two', 'start': 2, 'end': 4}]},
                    {'text': 'three four', 'start': 0, 'end': 4, 'timing_quality': 'word',
                     'words': [{'text': 'three', 'start': 0, 'end': 2}, {'text': 'four', 'start': 2, 'end': 4}]}]
        fixed, _ = normalize_segment_timing(original)
        words = timed_words(fixed)
        validate_segments(words)
        self.assertEqual(' '.join(w['text'] for w in words), 'one two three four')
        self.assertTrue(all(w['timing_quality'] == 'estimated' for w in words))

    def test_rounding_backstep_in_word_start_is_repaired_and_marked(self):
        words = timed_words([{'start': 0, 'end': 2, 'text': 'one two three',
                              'words': [{'text': 'one', 'start': 0.5, 'end': 1},
                                        {'text': 'two', 'start': 0.49, 'end': 1.1},
                                        {'text': 'three', 'start': 1.5, 'end': 2}]}])
        validate_segments(words)
        self.assertEqual(words[1]['timing_quality'], 'adjusted')

    def test_estimated_fallback_does_not_hide_invalid_word_numbers(self):
        seg = {'start': 2, 'end': 4, 'text': 'one two', 'timing_repaired': True,
               'words': [{'text': 'one', 'start': 0, 'end': 1},
                         {'text': 'two', 'start': float('nan'), 'end': 3}]}
        with self.assertRaises(ValueError):
            timed_words([seg])

    def test_long_story_with_overlapping_words_completes_every_batch(self):
        transcript = []
        for i in range(1063):
            start = i * 4.0
            transcript.append({'text': f'Описание{i} сцены{i}.', 'start': start, 'end': start + 4.04,
                               'words': [{'text': f'Описание{i}', 'start': start, 'end': start + 2.02},
                                         {'text': f'сцены{i}.', 'start': start + 2, 'end': start + 4.04}]})
        def respond(prompt):
            if '\nUNITS:\n' not in prompt:
                return {'summary': 'Long story', 'entities': []}
            units = json.loads(prompt.split('\nUNITS:\n')[1])
            return {'scenes': [{'first': u['id'], 'last': u['id'], 'kind': 'photo',
                                'subject': u['text'], 'query': u['text']} for u in units]}
        with patch('planner.llm_json', side_effect=respond):
            result = plan_story(transcript, {'analysis_mode':'ai'}, log=lambda _: None)
        validate_segments(result['segments'])
        self.assertEqual(len(result['segments']), 1063)
        self.assertEqual(' '.join(s['text'] for s in result['segments']), ' '.join(s['text'] for s in transcript))
        self.assertEqual(result['segments'][1061]['start'], 4244)
        self.assertEqual(result['segments'][-1]['end'], transcript[-1]['end'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_render_repaired_video_subtitles_and_sources_agree(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / 'voice.wav'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=frequency=440:sample_rate=48000:duration=5', str(audio)], check=True)
            segs = []
            for i, color in enumerate(('red', 'blue')):
                photo = root / f'{i}.png'
                Image.new('RGB', (320, 180), color).save(photo)
                segs.append({'start': 0.4 if i == 0 else 2, 'end': 3.8 if i == 0 else 4.6,
                             'text': f'Shot {i}', 'footage_path': str(photo), 'effect': 'none'})
            output = build_video(segs, audio, root / 'final.mp4',
                                 {'width': 320, 'height': 180, 'fps': 25, 'subtitles': True,
                                  'photo_layout': 'full_bleed', 'transition': 0}, log_fn=lambda _: None)
            info = probe(output)
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            self.assertEqual(int(video['nb_frames']), 125)
            self.assertTrue(any(s['codec_type'] == 'audio' for s in info['streams']))
            self.assertLess(abs(float(info['format']['duration']) - 5), 0.1)
            self.assertIn('00:00:00,400 --> 00:00:02,000', output.with_suffix('.srt').read_text())
            self.assertEqual(json.loads(output.with_suffix('.sources.json').read_text())[0]['end'], 2)
            for at, channel in ((1.8, 0), (2.2, 2)):
                pixel = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', str(output),
                                                  '-frames:v', '1', '-vf', 'crop=40:40:20:20,scale=1:1',
                                                  '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
                self.assertEqual(len(pixel), 3)
                self.assertGreater(pixel[channel], 180)
                self.assertLess(max(pixel[c] for c in range(3) if c != channel), 40)
            self.assertEqual(segs[0]['end'], 3.8)  # caller's project not silently mutated


if __name__ == '__main__':
    unittest.main()
