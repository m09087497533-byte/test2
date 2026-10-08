import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Item
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transcribe


class LocalTranscribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / 'voice.wav'
        self.audio.touch()
        self.items = [Item(text=' Ольга Корбут. ', start=0, end=3.1,
                           words=[Item(word=' Ольга', start=0, end=1), Item(word=' Корбут.', start=1, end=3.1)]),
                      Item(text=' Она выступила. ', start=3, end=6,
                           words=[Item(word=' Она', start=3, end=4), Item(word=' выступила.', start=4, end=6)])]

    def test_uses_local_engine_word_times_and_repairs_overlap_without_api_key(self):
        engine = Item(transcribe=lambda *args, **kwargs: (iter(self.items), Item(duration=6, language='ru')))
        logs = []
        with patch.object(transcribe, '_load_model', return_value=engine):
            result = transcribe.transcribe_audio(self.audio, split_scenes=False, progress_cb=logs.append)
        self.assertEqual(result['text'], 'Ольга Корбут. Она выступила.')
        self.assertEqual(result['segments'][0]['end'], 3)
        self.assertEqual(result['segments'][0]['words'][1]['start'], 1)
        self.assertEqual(result['segments'][0]['timing_quality'], 'word')
        self.assertTrue(any('100%' in line for line in logs))

    def test_cancelled_before_loading_never_downloads_model(self):
        with patch.object(transcribe, '_load_model') as load, self.assertRaisesRegex(RuntimeError, 'остановлено'):
            transcribe.transcribe_audio(self.audio, cancelled=lambda: True)
        load.assert_not_called()

    def test_cancelled_mid_stream_does_not_return_partial_transcription(self):
        stop = [False]
        def stream():
            yield self.items[0]
            stop[0] = True
            yield self.items[1]
        engine = Item(transcribe=lambda *args, **kwargs: (stream(), Item(duration=6, language='ru')))
        with patch.object(transcribe, '_load_model', return_value=engine), self.assertRaisesRegex(RuntimeError, 'прежний проект сохранён'):
            transcribe.transcribe_audio(self.audio, cancelled=lambda: stop[0])

    def test_empty_recognition_is_reported_before_project_update(self):
        engine = Item(transcribe=lambda *args, **kwargs: (iter([]), Item(duration=6, language='ru')))
        with patch.object(transcribe, '_load_model', return_value=engine), self.assertRaisesRegex(ValueError, 'не обнаружил речь'):
            transcribe.transcribe_audio(self.audio)


if __name__ == '__main__':
    unittest.main()
