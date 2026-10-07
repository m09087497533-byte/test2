"""Настольный единый редактор. Сеть и рендер работают вне потока Tk."""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))

import media
import render
import subtitles
import transcribe
import planner
from search_policy import SearchPolicy
from project import Project, PATTERNS

KINDS = {'По смыслу': 'auto', 'Фото': 'photo', 'Стоки': 'stock', 'YouTube': 'youtube', 'Интернет-видео': 'web'}
LABELS = {v: k for k, v in KINDS.items()}
LAYOUTS = {'Три портрета 9:16': 'portrait_triptych', 'Один портрет 9:16': 'portrait_card', 'Фото на весь кадр': 'full_bleed'}
EFFECTS = {'Появление снизу': 'slide_up', 'Появление сбоку': 'slide_left', 'Мягкое появление': 'fade',
           'Увеличение при появлении': 'pop', 'Плавный зум': 'slow_zoom', 'Без эффекта': 'none'}


def doctor():
    checks = {'python': sys.version.split()[0], 'ffmpeg': bool(shutil.which('ffmpeg')),
              'ffprobe': bool(shutil.which('ffprobe')), 'tesseract_optional': bool(shutil.which('tesseract')),
              'keys_present': {k: bool(os.getenv(k)) for k in ('FASTGEN_API_KEY', 'PEXELS_API_KEY', 'PIXABAY_API_KEY')}}
    runtime = Path(sys.executable).with_name('deno.exe' if sys.platform == 'win32' else 'deno')
    checks['youtube_js_runtime'] = bool(runtime.is_file() or shutil.which('deno') or shutil.which('node'))
    try:
        import tkinter
        checks['tkinter'] = tkinter.TkVersion
    except ImportError:
        checks['tkinter'] = False
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks['ffmpeg'] and checks['ffprobe'] and checks['tkinter'] else 1


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk, self.ttk, self.dialog, self.message = tk, ttk, filedialog, messagebox
        self.root = root
        self.project = None
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.controls = []
        self.selected = None
        self.candidate = None
        self.preview_ref = None
        root.title('SceneMix 2 — монтаж по рассказу')
        root.geometry('1280x940')
        root.minsize(1080, 780)
        root.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill='both', expand=True)
        top = ttk.Frame(outer)
        top.pack(fill='x')
        self.button(top, 'Открыть озвучку', self.pick_audio).pack(side='left')
        self.button(top, 'Транскрибировать', self.transcribe_audio).pack(side='left', padx=6)
        self.button(top, 'Импорт SRT / VTT', self.import_subtitles).pack(side='left')
        self.button(top, 'API-ключи', self.key_settings).pack(side='left', padx=6)
        self.audio_label = ttk.Label(top, text='Выберите аудио')
        self.audio_label.pack(side='left', padx=12)
        settings = ttk.LabelFrame(outer, text='Монтаж', padding=8)
        settings.pack(fill='x', pady=10)
        self.pattern = tk.StringVar(value='По смыслу рассказа')
        self.button(settings, 'Смысловой план', self.make_plan).pack(side='left', padx=5)
        ttk.Label(settings, text='Фото 6–14с · видео до 8с').pack(side='left', padx=5)
        self.resolution = tk.StringVar(value='1280×720')
        self.combo(settings, self.resolution, ['1280×720', '1920×1080', '720×1280'], 13).pack(side='left', padx=4)
        ttk.Label(settings, text='Затемнение, сек:').pack(side='left')
        self.fade = tk.StringVar(value='0')
        fade_entry = ttk.Entry(settings, textvariable=self.fade, width=5)
        fade_entry.pack(side='left', padx=4)
        self.controls.append((fade_entry, 'normal'))
        self.burn_subs = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(settings, text='Субтитры', variable=self.burn_subs)
        check.pack(side='left', padx=4)
        self.controls.append((check, 'normal'))
        self.external = tk.BooleanVar(value=False)
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=(0, 5))
        self.layout = tk.StringVar(value='Три портрета 9:16')
        self.effect = tk.StringVar(value='Появление снизу')
        self.combo(row, self.layout, list(LAYOUTS), 22).pack(side='left')
        self.combo(row, self.effect, list(EFFECTS), 24).pack(side='left', padx=5)
        self.names_on = tk.BooleanVar(value=True)
        check = ttk.Checkbutton(row, text='Имена при произнесении', variable=self.names_on)
        check.pack(side='left', padx=6)
        self.controls.append((check, 'normal'))
        self.button(row, 'Редактор имён и таймкодов', self.edit_names).pack(side='left')
        check = ttk.Checkbutton(outer, text='Разрешить загрузку веб / YouTube-материалов, которые я вправе использовать',
                                variable=self.external)
        check.pack(anchor='w')
        self.controls.append((check, 'normal'))
        body = ttk.Panedwindow(outer, orient='horizontal')
        body.pack(fill='both', expand=True, pady=8)
        left, right = ttk.Frame(body), ttk.Frame(body, padding=(12, 0))
        body.add(left, weight=1)
        body.add(right, weight=2)
        self.tree = ttk.Treeview(left, columns=('time', 'kind', 'status', 'text'), show='headings', selectmode='browse')
        for key, text, width in [('time', 'Начало', 60), ('kind', 'План', 80), ('status', 'Файл', 55), ('text', 'Озвучка', 220)]:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, stretch=key == 'text')
        scrollbar = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_scene)
        self.scene_label = ttk.Label(right, text='Выберите сцену', wraplength=590)
        self.scene_label.pack(fill='x', pady=5)
        self.file_label = ttk.Label(right, text='', wraplength=590)
        self.file_label.pack(fill='x')
        row = ttk.Frame(right)
        row.pack(fill='x', pady=7)
        self.kind = tk.StringVar(value='Фото')
        self.combo(row, self.kind, list(KINDS), 18).pack(side='left')
        self.query = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.query)
        entry.pack(side='left', fill='x', expand=True, padx=5)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Найти', self.search).pack(side='left')
        ttk.Label(right, text='Запрос можно задать вручную; пустое поле — подбор по смыслу.', wraplength=590).pack(anchor='w')
        self.candidates = ttk.Treeview(right, columns=('provider', 'score', 'title'), show='headings', height=5, selectmode='browse')
        self.candidates.heading('provider', text='Источник')
        self.candidates.heading('title', text='Название / запрос')
        self.candidates.heading('score', text='Смысл')
        self.candidates.column('score', width=55, stretch=False)
        self.candidates.column('provider', width=95, stretch=False)
        self.candidates.column('title', width=350)
        self.candidates.pack(fill='x', pady=8)
        self.candidates.bind('<<TreeviewSelect>>', self.select_candidate)
        self.preview = ttk.Label(right, text='Превью')
        self.preview.pack(anchor='w')
        row = ttk.Frame(right)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text='Начало клипа, сек:').pack(side='left')
        self.source_start = tk.StringVar(value='0')
        entry = ttk.Entry(row, textvariable=self.source_start, width=9)
        entry.pack(side='left', padx=5)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Таймкод по субтитрам', self.find_start).pack(side='left')
        self.match_label = ttk.Label(right, text='', wraplength=590)
        self.match_label.pack(fill='x')
        row = ttk.Frame(right)
        row.pack(fill='x', pady=6)
        self.button(row, 'Назначить найденное', self.assign_candidate).pack(side='left')
        self.button(row, 'Открыть источник', self.open_source).pack(side='left', padx=5)
        self.button(row, 'Свой файл', self.local_file).pack(side='left')
        row = ttk.Frame(right)
        row.pack(fill='x', pady=5)
        self.url = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.url)
        entry.pack(side='left', fill='x', expand=True)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Добавить ссылку на видео', self.add_url).pack(side='left', padx=5)
        row = ttk.Frame(right)
        row.pack(fill='x', pady=4)
        self.button(row, 'Сохранить начало', self.save_offset).pack(side='left')
        self.button(row, 'Снять назначение', self.clear_scene).pack(side='left', padx=5)
        self.button(row, 'Открыть файл', self.play_file).pack(side='left')
        self.button(row, 'Стиль сцены', self.scene_style).pack(side='left', padx=5)
        bottom = ttk.Frame(outer)
        bottom.pack(fill='x')
        self.button(bottom, 'Подобрать все пустые сцены', self.auto_pick).pack(side='left')
        self.stop_button = ttk.Button(bottom, text='Остановить подбор', command=self.stop.set, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        self.button(bottom, 'Собрать видео', self.render).pack(side='left')
        self.button(bottom, 'Превью сцены', self.preview_scene).pack(side='left', padx=5)
        self.status = ttk.Label(bottom, text='Готов к работе')
        self.status.pack(side='left', padx=10)
        self.log_box = tk.Text(outer, height=6, state='disabled', wrap='word')
        self.log_box.pack(fill='x', pady=(8, 0))
        self.root.after(100, self.poll)

    def button(self, parent, text, command):
        def guarded():
            if self.busy:
                return
            try:
                command()
            except Exception as exc:
                self.message.showerror('Ошибка', str(exc))
        widget = self.ttk.Button(parent, text=text, command=guarded)
        self.controls.append((widget, 'normal'))
        return widget

    def combo(self, parent, variable, values, width):
        widget = self.ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=width)
        self.controls.append((widget, 'readonly'))
        return widget

    def log(self, text):
        self.events.put(('log', str(text)))

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    self.log_box.config(state='normal')
                    self.log_box.insert('end', payload + '\n')
                    self.log_box.see('end')
                    self.log_box.config(state='disabled')
                elif kind == 'done':
                    self.busy = False
                    for widget, state in self.controls:
                        widget.configure(state=state)
                    self.stop_button.configure(state='disabled')
                    self.refresh()
                    if payload:
                        payload()
                elif kind == 'error':
                    self.message.showerror('Операция не завершена', payload)
                elif kind == 'preview':
                    token, image = payload
                    if self.candidate and token == self.candidate['id']:
                        from PIL import ImageTk
                        self.preview_ref = ImageTk.PhotoImage(image)
                        self.preview.configure(image=self.preview_ref, text='')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def task(self, label, function, done=None, stoppable=False):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.status.configure(text=label)
        for widget, _ in self.controls:
            widget.configure(state='disabled')
        if stoppable:
            self.stop_button.configure(state='normal')
        def work():
            success = False
            try:
                function()
                success = True
            except Exception as exc:
                self.log(str(exc))
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('done', done if success else None))
        threading.Thread(target=work, daemon=True).start()

    def require_scene(self):
        if not self.project or self.selected is None:
            raise ValueError('Сначала выберите аудио и сцену в списке.')
        return self.project.segments[self.selected]

    def settings(self):
        if not self.project:
            raise ValueError('Сначала выберите аудио.')
        width, height = self.resolution.get().split('×')
        fade = float(self.fade.get().replace(',', '.'))
        if not 0 <= fade <= 2:
            raise ValueError('Затемнение: от 0 до 2 секунд')
        self.project.settings.update(width=int(width), height=int(height), transition=fade,
                                     subtitles=self.burn_subs.get(), allow_external=self.external.get(),
                                     photo_layout=LAYOUTS[self.layout.get()], photo_effect=EFFECTS[self.effect.get()],
                                     name_titles_enabled=self.names_on.get())
        self.project.save()

    def pick_audio(self):
        name = self.dialog.askopenfilename(filetypes=[('Аудио', '*.mp3 *.wav *.m4a *.aac *.flac *.ogg'), ('Все', '*.*')])
        if not name:
            return
        project = Project(name)
        project.load()
        project.ensure_dirs()
        self.project, self.selected, self.candidate = project, None, None
        self.pattern.set(project.settings['pattern'])
        self.resolution.set(f"{project.settings['width']}×{project.settings['height']}")
        self.fade.set(str(project.settings['transition']))
        self.burn_subs.set(project.settings['subtitles'])
        self.external.set(project.settings['allow_external'])
        self.layout.set(next(k for k, v in LAYOUTS.items() if v == project.settings['photo_layout']))
        self.effect.set(next(k for k, v in EFFECTS.items() if v == project.settings['photo_effect']))
        self.names_on.set(project.settings['name_titles_enabled'])
        self.audio_label.configure(text=Path(name).name)
        self.scene_label.configure(text='Выберите сцену')
        self.file_label.configure(text='')
        self.candidates.delete(*self.candidates.get_children())
        self.preview.configure(image='', text='Превью')
        self.refresh()

    def refresh(self):
        if not self.project:
            return
        if self.selected is not None and self.selected >= len(self.project.segments):
            self.selected, self.candidate = None, None
            self.scene_label.configure(text='Выберите сцену')
            self.file_label.configure(text='')
            self.candidates.delete(*self.candidates.get_children())
        desired_ids = [str(i) for i in range(len(self.project.segments))]
        if list(self.tree.get_children()) != desired_ids:
            self.tree.delete(*self.tree.get_children())
        done = 0
        for i, seg in enumerate(self.project.segments):
            has_file = bool(seg.get('footage_path') and Path(seg['footage_path']).is_file())
            done += has_file
            values = (f"{seg['start']:.1f}", LABELS.get(seg.get('desired_kind'), '?'),
                      '✓' if has_file else '—', seg['text'])
            if self.tree.exists(str(i)):
                self.tree.item(str(i), values=values)
            else:
                self.tree.insert('', 'end', iid=str(i), values=values)
        self.status.configure(text=f'Назначено {done}/{len(self.project.segments)}')
        if self.selected is not None and self.selected < len(self.project.segments):
            if self.tree.selection() != (str(self.selected),):
                self.tree.selection_set(str(self.selected))
            seg = self.project.segments[self.selected]
            self.file_label.configure(text=f"Файл: {seg.get('footage_path') or 'не назначен'}")

    def import_subtitles(self):
        self.settings()
        name = self.dialog.askopenfilename(filetypes=[('Субтитры', '*.srt *.vtt')])
        if not name:
            return
        project = self.project
        def work():
            result = subtitles.import_subtitle_file(name)
            project.set_segments_from_transcript(result['segments'])
            project.raw_transcript = result['segments']
            project.name_titles = []
            project.save()
            self.log(f'Импортировано сцен: {len(project.segments)}')
        self.task('Импортирую…', work)

    def transcribe_audio(self):
        self.settings()
        project = self.project
        def work():
            result = transcribe.transcribe_audio(project.audio_path, progress_cb=self.log, split_scenes=False)
            project.set_segments_from_transcript(result['segments'])
            project.raw_transcript = result['segments']
            project.name_titles = []
            project.save()
        self.task('Транскрибирую…', work)

    def make_plan(self):
        self.settings()
        project = self.project
        if not project.segments:
            raise ValueError('Сначала транскрибируйте аудио или импортируйте субтитры.')
        def work():
            result = planner.plan_story(project.raw_transcript or project.segments,
                                        project.settings, self.log, self.stop.is_set)
            planner.save_plan(project, result)
            self.log(f'План: {len(project.segments)} сцен, {len(project.name_titles)} именных титров. '
                     'Старый project.json сохранён резервной копией.')
        self.task('Планирую по рассказу…', work, done=self.select_scene, stoppable=True)

    def apply_pattern(self):
        self.settings()
        self.project.apply_pattern(self.pattern.get())
        self.project.save()
        self.refresh()
        self.log('План чередования обновлён. Уже назначенные файлы сохранены.')

    def select_scene(self, _event=None):
        if self.busy:
            return
        selection = self.tree.selection()
        if not selection or not self.project:
            return
        self.selected = int(selection[0])
        seg = self.require_scene()
        self.candidate = None
        self.scene_label.configure(text=f"Сцена {self.selected + 1} · {seg['start']:.2f}–{seg['end']:.2f}с\n{seg['text']}")
        self.file_label.configure(text=f"Файл: {seg.get('footage_path') or 'не назначен'}")
        self.kind.set(LABELS.get(seg.get('desired_kind'), 'По смыслу'))
        self.query.set(seg.get('search_query', ''))
        self.source_start.set(str(seg.get('source_start', 0)))
        self.match_label.configure(text='')
        if seg.get('visual_reason'):
            self.match_label.configure(text=seg['visual_reason'])
        self.preview.configure(image='', text='Превью')
        self.fill_candidates()

    def fill_candidates(self):
        seg = self.require_scene()
        self.candidates.delete(*self.candidates.get_children())
        for i, candidate in enumerate(seg.get('candidates', [])):
            score = candidate.get('relevance_score')
            self.candidates.insert('', 'end', iid=str(i), values=(candidate['provider'],
                f'{score:.0%}' if score is not None else '—', candidate.get('title') or candidate.get('query', '')))

    def select_candidate(self, _event=None):
        if self.busy:
            return
        selection = self.candidates.selection()
        if not selection:
            return
        candidate = self.require_scene()['candidates'][int(selection[0])]
        self.candidate = candidate
        self.source_start.set('0')
        self.preview.configure(image='', text='Превью недоступно')
        url = candidate.get('preview_image')
        if not url:
            return
        def work():
            try:
                import io
                from PIL import Image, ImageOps
                response = media.requests.get(media.safe_url(url), timeout=12)
                response.raise_for_status()
                with Image.open(io.BytesIO(response.content)) as image:
                    image = ImageOps.exif_transpose(image).convert('RGB')
                    image.thumbnail((360, 160))
                    self.events.put(('preview', (candidate['id'], image.copy())))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def search(self):
        seg = self.require_scene()
        project = self.project
        new_query = self.query.get().strip()
        if new_query != seg.get('search_query', ''):
            seg['manual_query'] = True
        seg.update(desired_kind=KINDS[self.kind.get()], search_query=new_query)
        project.save()
        context = ' '.join(s['text'] for s in project.segments[max(0, self.selected - 1):self.selected + 2])
        def work():
            candidates, warnings = media.search(seg, context=context, policy=SearchPolicy(project.project_dir))
            seg['candidates'] = candidates
            project.save()
            for warning in warnings:
                self.log(warning)
            self.log(f'Найдено вариантов: {len(candidates)}')
        self.task('Ищу…', work, self.fill_candidates)

    def add_url(self):
        seg = self.require_scene()
        url = media.safe_url(self.url.get().strip())
        candidate = {'id': 'link_' + media.stable_id(url), 'provider': 'webvideo',
                     'source_url': url, 'video_url': url, 'media_kind': 'video', 'title': url,
                     'query': self.query.get().strip()}
        seg.setdefault('candidates', []).append(candidate)
        self.project.save()
        self.fill_candidates()
        self.candidates.selection_set(str(len(seg['candidates']) - 1))
        self.select_candidate()

    def find_start(self):
        seg = self.require_scene()
        candidate = self.candidate
        if not candidate or candidate['provider'] not in ('youtube', 'webvideo'):
            raise ValueError('Выберите YouTube / веб-видео.')
        result = {}
        def work():
            result['match'] = media.suggest_start(candidate, seg)
        def done():
            match = result['match']
            if match:
                self.source_start.set(str(round(match['start'], 3)))
                self.match_label.configure(text='Совпадение слов в субтитрах: ' + match['excerpt'])
            else:
                self.match_label.configure(text='Субтитры или совпадения не найдены. Задайте начало вручную.')
        self.task('Ищу таймкод…', work, done)

    def assign_candidate(self):
        seg = self.require_scene()
        if not self.candidate:
            raise ValueError('Выберите вариант из результатов поиска.')
        self.settings()
        candidate, project, index = dict(self.candidate), self.project, self.selected
        start = float(self.source_start.get().replace(',', '.'))
        def work():
            path = media.download(candidate, project.media_dir, seg['end'] - seg['start'],
                                  start, project.settings['allow_external'])
            is_stock = candidate['provider'] in ('pexels', 'pixabay')
            project.assign(index, path, candidate, start if is_stock else 0)
            seg['original_source_start'] = start
            seg['source_clip_duration'] = min(8.0, seg['end'] - seg['start'])
            seg['review_status'] = 'manually_selected'
            project.save()
            self.log('Назначено: ' + str(path))
        self.task('Загружаю…', work)

    def open_source(self):
        self.require_scene()
        url = (self.candidate or {}).get('source_url') or self.require_scene().get('source_url')
        if url:
            webbrowser.open(media.safe_url(url))
        else:
            raise ValueError('У файла нет ссылки на источник.')

    def local_file(self):
        self.require_scene()
        name = self.dialog.askopenfilename(filetypes=[('Фото и видео', '*.jpg *.jpeg *.png *.webp *.bmp *.mp4 *.mov *.mkv *.avi *.webm'), ('Все', '*.*')])
        if name:
            self.project.assign(self.selected, name)
            self.source_start.set('0')
            self.refresh()

    def save_offset(self):
        seg = self.require_scene()
        if not seg.get('footage_path'):
            raise ValueError('Сначала назначьте файл.')
        self.project.assign(self.selected, seg['footage_path'],
                            {'provider': seg.get('footage_source', 'local'),
                             'source_url': seg.get('source_url'), 'license_url': seg.get('license_url'),
                             'title': seg.get('title'), 'query': seg.get('footage_query'),
                             'id': seg.get('selected_candidate_id')},
                            float(self.source_start.get().replace(',', '.')))
        self.log('Начало локального фрагмента сохранено.')

    def clear_scene(self):
        seg = self.require_scene()
        for key in ('footage_path', 'media_kind', 'source_url', 'license_url', 'original_source_start',
                    'footage_source', 'footage_query', 'selected_candidate_id'):
            seg.pop(key, None)
        seg['source_start'] = 0
        self.project.save()
        self.refresh()

    def play_file(self):
        seg = self.require_scene()
        path = seg.get('footage_path')
        if not path or not Path(path).is_file():
            raise ValueError('Сначала назначьте файл.')
        if sys.platform == 'win32':
            os.startfile(path)
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def auto_pick(self):
        self.settings()
        if not self.project.segments:
            raise ValueError('Сначала импортируйте субтитры или транскрибируйте озвучку.')
        project = self.project
        def work():
            if not project.story and any(s.get('footage_path') for s in project.segments):
                raise ValueError('Это проект без смыслового плана. Нажмите «Смысловой план», '
                                 'чтобы пересобрать сцены; предыдущий проект сохранится резервной копией.')
            if any(s.get('desired_kind') == 'auto' for s in project.segments):
                result = planner.plan_story(project.raw_transcript or project.segments,
                                            project.settings, self.log, self.stop.is_set)
                planner.save_plan(project, result)
            media.auto_pick(project, self.log, self.stop.is_set)
        self.task('План и подбор…', work, done=self.select_scene, stoppable=True)

    def render(self):
        self.settings()
        project = self.project
        output = project.project_dir / 'final_video.mp4'
        self.task('Собираю…', lambda: render.build_video(project.segments, project.audio_path, output,
                   project.settings, self.log, name_titles=project.name_titles),
                  lambda: self.message.showinfo('Готово', f'Видео сохранено:\n{output}\n\nРядом: субтитры и список источников.'))

    def key_settings(self):
        from dotenv import dotenv_values, set_key
        path = Path(__file__).with_name('.env')
        values = dotenv_values(path) if path.exists() else {}
        win = self.tk.Toplevel(self.root)
        win.title('API-ключи — хранятся локально в .env')
        win.transient(self.root)
        win.grab_set()
        frame = self.ttk.Frame(win, padding=16)
        frame.pack(fill='both', expand=True)
        variables = {}
        for i, key in enumerate(('FASTGEN_API_KEY', 'PEXELS_API_KEY', 'PIXABAY_API_KEY')):
            self.ttk.Label(frame, text=key).grid(row=i, column=0, sticky='w', pady=5)
            variable = self.tk.StringVar(value=values.get(key) or os.getenv(key, ''))
            variables[key] = variable
            self.ttk.Entry(frame, textvariable=variable, show='•', width=44).grid(row=i, column=1, padx=8, pady=5)
        self.ttk.Label(frame, text='Fast-gen: план и транскрибация. Pexels/Pixabay: только стоки.\n'
                       'Ключи не отправляются в GitHub и не выводятся в журнал.').grid(row=3, column=0, columnspan=2, pady=8)
        def save():
            if not path.exists():
                path.touch(mode=0o600)
            for key, variable in variables.items():
                value = variable.get().strip()
                set_key(str(path), key, value)
                os.environ[key] = value
                for module in (media.photo, media.footage, transcribe):
                    if hasattr(module, key):
                        setattr(module, key, value or None)
            if os.name != 'nt':
                path.chmod(0o600)
            self.log('Настройки ключей сохранены локально. Значения скрыты.')
            win.destroy()
        self.ttk.Button(frame, text='Сохранить', command=save).grid(row=4, column=1, sticky='e')

    def scene_style(self):
        seg = self.require_scene()
        win = self.tk.Toplevel(self.root)
        win.title('Оформление выбранной сцены')
        win.transient(self.root)
        win.grab_set()
        frame = self.ttk.Frame(win, padding=14)
        frame.pack()
        layout = self.tk.StringVar(value=next((k for k, v in LAYOUTS.items() if v == seg.get('photo_layout')), self.layout.get()))
        effect = self.tk.StringVar(value=next((k for k, v in EFFECTS.items() if v == seg.get('effect')), self.effect.get()))
        self.ttk.Combobox(frame, textvariable=layout, values=list(LAYOUTS), state='readonly', width=28).pack(pady=5)
        self.ttk.Combobox(frame, textvariable=effect, values=list(EFFECTS), state='readonly', width=28).pack(pady=5)
        def save():
            seg.update(photo_layout=LAYOUTS[layout.get()], effect=EFFECTS[effect.get()])
            self.project.save()
            win.destroy()
        self.ttk.Button(frame, text='Сохранить для этой сцены', command=save).pack(pady=5)

    def edit_names(self):
        if not self.project:
            raise ValueError('Сначала выберите озвучку.')
        import copy
        import math
        project = self.project
        rows = copy.deepcopy(project.name_titles)
        win = self.tk.Toplevel(self.root)
        win.title('Имена: появление в момент произнесения')
        win.transient(self.root)
        win.grab_set()
        frame = self.ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        tree = self.ttk.Treeview(frame, columns=('name', 'start', 'end', 'quality'), show='headings', height=8, selectmode='browse')
        for key, label, width in [('name', 'Имя фамилия', 230), ('start', 'Секунда появления', 140),
                                   ('end', 'Секунда исчезновения', 160), ('quality', 'Точность', 140)]:
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.pack(fill='both', expand=True)
        name, start, end = self.tk.StringVar(), self.tk.StringVar(value='0'), self.tk.StringVar(value='3.8')
        row = self.ttk.Frame(frame)
        row.pack(fill='x', pady=8)
        for label, variable, width in [('Имя', name, 28), ('Начало', start, 9), ('Конец', end, 9)]:
            self.ttk.Label(row, text=label).pack(side='left', padx=4)
            self.ttk.Entry(row, textvariable=variable, width=width).pack(side='left')
        def refresh():
            tree.delete(*tree.get_children())
            for i, title in enumerate(rows):
                quality = {'word': 'По словам API', 'estimated': 'Примерный', 'manual': 'Ручной'}.get(title.get('timing_quality'), 'Примерный')
                tree.insert('', 'end', iid=str(i), values=(title['name'], f"{title['start']:.3f}", f"{title['end']:.3f}", quality))
        def choose(_event=None):
            if tree.selection():
                title = rows[int(tree.selection()[0])]
                name.set(title['name']); start.set(str(title['start'])); end.set(str(title['end']))
        tree.bind('<<TreeviewSelect>>', choose)
        def change(replace):
            try:
                first, last = float(start.get().replace(',', '.')), float(end.get().replace(',', '.'))
                value = name.get().strip()
                if not value or len(value) > 120 or not all(math.isfinite(v) for v in (first, last)) or first < 0 or last <= first:
                    raise ValueError('Введите имя и корректный интервал в секундах.')
                title = {'name': value, 'start': first, 'end': last, 'timing_quality': 'manual'}
                if replace and tree.selection():
                    rows[int(tree.selection()[0])] = title
                else:
                    rows.append(title)
                refresh()
            except ValueError as exc:
                self.message.showerror('Ошибка титра', str(exc), parent=win)
        def remove():
            if tree.selection():
                rows.pop(int(tree.selection()[0])); refresh()
        def save():
            project.name_titles = sorted(rows, key=lambda t: t['start'])
            project.save()
            win.destroy()
        row = self.ttk.Frame(frame)
        row.pack(fill='x')
        for label, action in [('Добавить', lambda: change(False)), ('Изменить выбранное', lambda: change(True)),
                               ('Удалить', remove), ('Сохранить титры', save)]:
            self.ttk.Button(row, text=label, command=action).pack(side='left', padx=4)
        self.ttk.Label(frame, text='После SRT тайминг слов приблизительный; проверьте его в превью.\n'
                       'После API с word timestamps начало берётся из времени произнесённого имени.').pack(anchor='w', pady=8)
        refresh()

    def preview_scene(self):
        import copy
        seg = copy.deepcopy(self.require_scene())
        self.settings()
        project = self.project
        first = float(seg['start'])
        length = min(8.0, float(seg['end']) - first)
        source_audio = project.project_dir / 'preview_audio.wav'
        output = project.project_dir / 'preview_scene.mp4'
        def work():
            render.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(first), '-i', str(project.audio_path),
                        '-t', str(length), '-vn', str(source_audio)])
            seg.update(start=0, end=length)
            names = [{**t, 'start': max(0, t['start'] - first), 'end': min(length, t['end'] - first)}
                     for t in project.name_titles if t['start'] < first + length and t['end'] > first]
            settings = dict(project.settings)
            settings.update(width=640 if settings['width'] > settings['height'] else 360,
                            height=360 if settings['width'] > settings['height'] else 640)
            render.build_video([seg], source_audio, output, settings, self.log, name_titles=names)
        def done():
            if sys.platform == 'win32':
                os.startfile(str(output))
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(output)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log('Превью: ' + str(output))
        self.task('Превью выбранной сцены…', work, done)

    def close(self):
        if self.busy:
            self.message.showinfo('Выполняется операция', 'Остановите автоподбор или дождитесь завершения текущей операции.')
            return
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='SceneMix — смешанный видеоредактор')
    parser.add_argument('--doctor', action='store_true', help='Проверить инструменты и наличие ключей без значений')
    parser.add_argument('--render', metavar='AUDIO', help='Собрать сохранённый проект без GUI')
    parser.add_argument('--autopick', metavar='AUDIO', help='Подобрать пустые сцены сохранённого проекта без GUI')
    args = parser.parse_args()
    if args.doctor:
        return doctor()
    if args.render or args.autopick:
        project = Project(args.render or args.autopick)
        if not project.load():
            parser.error('Сохранённый проект не найден')
        if args.autopick:
            media.auto_pick(project)
            return 0 if all(s.get('footage_path') and Path(s['footage_path']).is_file() for s in project.segments) else 1
        render.build_video(project.segments, project.audio_path, project.project_dir / 'final_video.mp4',
                           project.settings, name_titles=project.name_titles)
        return 0
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print('Не удалось открыть окно. Нужен графический рабочий стол. Для проверки: python main.py --doctor.', file=sys.stderr)
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
