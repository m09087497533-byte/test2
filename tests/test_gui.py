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
        verifier = patch('media.visual_match.verify_file', return_value={
            'compatible': True, 'relevance_score': 0.9, 'relevance_reason': 'Verified test fixture',
            'visual_verified': True, 'relevance_basis': 'downloaded_visual_and_metadata'})
        verifier.start()
        self.addCleanup(verifier.stop)
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

    def test_open_old_project_repairs_overlap_without_popup_or_losing_media(self):
        from PIL import Image
        photo = Path(self.tmp.name) / 'ready.png'
        Image.new('RGB', (160, 90), 'green').save(photo)
        project = self.app.project
        project.segments = [{'text': 'one', 'start': 0, 'end': 4.2, 'footage_path': str(photo)},
                            {'text': 'two', 'start': 4, 'end': 8, 'footage_path': str(photo)}]
        project.save()
        with patch.object(self.app.dialog, 'askopenfilename', return_value=str(project.audio_path)), \
             patch.object(self.app.message, 'showerror') as error:
            self.app.pick_audio()
            self.root.update()
        error.assert_not_called()
        self.assertEqual(self.app.project.segments[0]['end'], 4)
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.assertTrue(all(s['footage_path'] == str(photo) for s in self.app.project.segments))
        self.assertTrue(list(project.project_dir.glob('project.before-timing.*.json')))

    def test_global_source_choice_is_persisted_and_clears_previous_exceptions(self):
        self.app.project.segments[0]['source_override'] = 'photo'
        self.app.source_mode.set('Стоки')
        self.app.change_source_mode()
        self.root.update()
        self.assertEqual(self.app.project.settings['source_mode'], 'stock')
        self.assertNotIn('source_override', self.app.project.segments[0])
        self.assertEqual(self.app.kind.get(), 'Стоки')
        self.assertEqual(self.app.tree.item('0', 'values')[1], 'Стоки')

    def test_audio_automatically_imports_sidecar_subtitles_without_recognition(self):
        audio = Path(self.tmp.name) / 'new_voice.wav'
        audio.with_suffix('.srt').write_text(
            '1\n00:00:00,000 --> 00:00:06,000\nОльга Корбут выступила.\n', encoding='utf-8')
        with patch.object(self.app.dialog, 'askopenfilename', return_value=str(audio)), \
             patch.object(self.app.message, 'showerror') as error:
            self.app.pick_audio()
            self.root.update()
        error.assert_not_called()
        self.assertEqual(self.app.project.raw_transcript[0]['text'], 'Ольга Корбут выступила.')
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertTrue(self.app.project.project_file.exists())

    def test_existing_transcript_is_preserved_even_when_sidecar_differs(self):
        self.app.project.audio_path.with_suffix('.srt').write_text(
            '1\n00:00:00,000 --> 00:00:06,000\nДругой текст.\n', encoding='utf-8')
        self.app.project.save()
        with patch.object(self.app.dialog, 'askopenfilename', return_value=str(self.app.project.audio_path)):
            self.app.pick_audio()
            self.root.update()
        self.assertEqual(self.app.project.segments[0]['text'], 'train')

    def test_toolbar_offers_original_file_workflow_without_ai_or_model_download(self):
        labels = [widget.cget('text') for widget in self.app.toolbar_items]
        self.assertIn('Ключи стоков', labels)
        self.assertFalse(any('AI' in label or 'Распознать' in label for label in labels))

    def test_scene_source_choice_persists_before_search_button_is_pressed(self):
        from project import Project
        self.app.kind.set('Стоки')
        self.app.change_scene_source()
        self.root.update()
        loaded = Project(self.app.project.audio_path)
        loaded.load()
        self.assertEqual(loaded.segments[0]['source_override'], 'stock')
        self.assertTrue(loaded.segments[0]['source_query_dirty'])

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

    def test_search_button_assigns_photo_without_selecting_a_card(self):
        from PIL import Image
        image = Path(self.tmp.name) / 'downloaded.png'
        Image.new('RGB', (160, 90), 'green').save(image)
        self.app.kind.set('Фото')
        self.app.query.set('steam train')
        candidate = {'id': 'auto', 'provider': 'web', 'media_kind': 'photo', 'relevance_score': 0.55}
        with patch('main.media.search', return_value=([candidate], [])), \
             patch('main.media.download', return_value=image), patch.object(self.app.message, 'showerror') as error:
            self.app.search()
            self.wait_task()
        error.assert_not_called()
        scene = self.app.project.segments[0]
        self.assertEqual(scene['selected_candidate_id'], 'auto')
        self.assertTrue(Path(scene['footage_path']).is_file())
        self.assertEqual(scene['auto_pick_status'], 'assigned')

    def test_render_button_fills_missing_scenes_before_rendering(self):
        from PIL import Image
        image = Path(self.tmp.name) / 'ready.png'
        Image.new('RGB', (160, 90), 'green').save(image)
        calls = []
        def autopick(project, *args):
            calls.append('pick')
            project.assign(0, image)
        def render(*args, **kwargs):
            calls.append('render')
            self.assertTrue(Path(args[0][0]['footage_path']).is_file())
        with patch('main.plan_and_pick', side_effect=autopick), \
             patch('main.render.build_video', side_effect=render), patch.object(self.app.message, 'showinfo') as done:
            self.app.render()
            self.wait_task()
        self.assertEqual(calls, ['pick', 'render'])
        done.assert_called_once()


if __name__ == '__main__':
    unittest.main()
