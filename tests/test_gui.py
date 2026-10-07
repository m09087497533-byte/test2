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
        for timer in self.root.tk.call('after', 'info'):
            self.root.after_cancel(timer)
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

    def test_cards_keep_selection_and_discard_previous_scene_thumbnail(self):
        from PIL import Image
        self.app.project.segments[0]['candidates'] = [
            {'id': 'one', 'provider': 'local', 'title': 'Первый вариант'},
            {'id': 'two', 'provider': 'local', 'title': 'Второй вариант'}]
        self.app.fill_candidates()
        self.root.update()
        self.app.choose_card(1)
        self.app.source_start.set('12.5')
        self.root.update()  # Queued TreeviewSelect must not reset the manually entered time.
        self.assertEqual(self.app.candidate['id'], 'two')
        self.assertEqual(self.app.source_start.get(), '12.5')
        old_generation = self.app.card_generation
        previous_buttons = [card['button'] for card in self.app.card_widgets.values()]
        self.app.clear_cards()
        self.app.events.put(('card_image', (old_generation, 1, Image.new('RGB', (200, 100)))))
        self.app.poll()
        self.assertFalse(self.app.card_images)
        self.assertTrue(all(widget not in previous_buttons for widget, _ in self.app.controls))

    def test_compact_window_keeps_assignment_controls_and_log_visible(self):
        self.root.deiconify()
        self.root.geometry('1280x850')
        self.app.project.segments[0]['candidates'] = [{'id': 'one', 'provider': 'local', 'title': 'Фото'}]
        self.app.fill_candidates()
        self.root.update()
        for widget in [self.app.log_box, self.app.cards_canvas] + [w for w, _ in self.app.controls
                if w.winfo_class() == 'TButton' and w.cget('text') in ('Назначить выбранное', 'Превью сцены', 'Собрать видео')]:
            self.assertTrue(widget.winfo_ismapped(), str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())


if __name__ == '__main__':
    unittest.main()
