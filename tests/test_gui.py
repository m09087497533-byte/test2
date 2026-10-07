import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.getenv('DISPLAY') or sys.platform in ('win32', 'darwin'), 'Нужен графический дисплей')
class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from main import App
        from project import Project
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.app.project = Project(Path(self.tmp.name) / 'voice.wav')
        self.app.project.set_segments_from_transcript([{'text': 'train', 'start': 0, 'end': 3}])
        self.app.refresh()
        self.root.update()
        self.app.tree.selection_set('0')
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()

    def wait_task(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def test_caption_match_updates_field_after_worker_and_refresh(self):
        self.assertEqual(self.app.selected, 0)
        self.app.candidate = {'id': 'abc', 'provider': 'youtube', 'source_url': 'https://youtube.com/watch?v=abc'}
        with patch('main.media.suggest_start', return_value={'start': 12.5, 'excerpt': 'steam train', 'score': 0.5}):
            self.app.find_start()
            self.wait_task()
        self.assertEqual(self.app.source_start.get(), '12.5')
        self.app.refresh()
        self.root.update()
        self.assertEqual(self.app.source_start.get(), '12.5')

    def test_worker_error_restores_controls(self):
        with patch.object(self.app.message, 'showerror') as showerror:
            self.app.task('test', lambda: (_ for _ in ()).throw(ValueError('test error')))
            self.wait_task()
            showerror.assert_called_once()
        self.assertTrue(all(str(widget['state']) == state for widget, state in self.app.controls))


if __name__ == '__main__':
    unittest.main()
