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
from project import Project, PATTERNS

KINDS = {'По смыслу': 'auto', 'Фото': 'photo', 'Стоки': 'stock', 'YouTube': 'youtube', 'Интернет-видео': 'web'}
LABELS = {v: k for k, v in KINDS.items()}
LAYOUTS = {'Авто: 16:9 или 9:16': 'auto', 'Три портрета 9:16': 'portrait_triptych',
           'Один портрет 9:16': 'portrait_card', 'Фото на весь кадр 16:9': 'full_bleed'}
EFFECTS = {'Появление снизу': 'slide_up', 'Появление сбоку': 'slide_left', 'Мягкое появление': 'fade',
           'Увеличение при появлении': 'pop', 'Плавный зум': 'slow_zoom', 'Без эффекта': 'none'}


def timecode(seconds):
    millis = round(float(seconds) * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}.{millis:03}'


def plan_and_pick(project, log=print, cancelled=lambda: False, progress=None):
    """One action for both a new transcript and an existing partially filled project."""
    if not project.story or any(s.get('desired_kind') == 'auto' for s in project.segments):
        import copy
        previous = copy.deepcopy(project.segments)
        result = planner.plan_story(project.raw_transcript or project.segments,
                                    project.settings, log, cancelled)
        assignment_keys = ('footage_path', 'media_kind', 'source_start', 'footage_source', 'footage_query',
                           'source_url', 'license_url', 'title', 'selected_candidate_id', 'asset_url',
                           'original_source_start', 'source_clip_duration', 'photo_fingerprint', 'review_status')
        for scene in result['segments']:
            old = next((s for s in previous if s.get('footage_path') and Path(s['footage_path']).is_file()
                        and s['text'] == scene['text'] and abs(s['start'] - scene['start']) < 0.001
                        and abs(s['end'] - scene['end']) < 0.001), None)
            if old:
                scene.update({k: old[k] for k in assignment_keys if k in old})
        planner.save_plan(project, result)
    return media.auto_pick(project, log, cancelled, progress=progress)


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
        import theme
        self.colors = theme
        self.style = theme.apply(root, ttk)
        self.card_generation = 0
        self.card_widgets = {}
        self.card_images = {}
        self.card_originals = {}
        root.title('SceneMix 3.1 — автоматический подбор · монтаж по рассказу')
        root.geometry(f'{min(1440, root.winfo_screenwidth() - 60)}x{min(960, root.winfo_screenheight() - 80)}')
        root.minsize(1080, 780)
        root.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill='both', expand=True)
        top = ttk.Frame(outer)
        top.pack(fill='x', pady=(0, 12))
        self.toolbar_items = [ttk.Label(top, text='SceneMix 3.1', style='Title.TLabel')]
        for label, command, color in [('Аудио', self.pick_audio, 'Blue'),
                ('Распознать', self.transcribe_audio, None), ('SRT / VTT', self.import_subtitles, None),
                ('API-ключи', self.key_settings, None), ('Смысловой план', self.make_plan, 'Peach'),
                ('Подобрать все', self.auto_pick, 'Purple'), ('Собрать видео', self.render, 'Green')]:
            self.toolbar_items.append(self.button(top, label, command, color))
        for i, widget in enumerate(self.toolbar_items):
            widget.grid(row=0, column=i, padx=(0, 6), pady=2, sticky='w')
        top.bind('<Configure>', self.resize_toolbar)
        counts = ttk.Frame(outer)
        counts.pack(fill='x', pady=(0, 10))
        self.audio_label = ttk.Label(counts, text='Аудио не выбрано', style='Muted.TLabel')
        self.audio_label.pack(side='left', padx=(0, 20))
        self.transcript_status = ttk.Label(counts, text='Транскрипция: —', style='Success.TLabel')
        self.transcript_status.pack(side='left', padx=12)
        self.media_status = ttk.Label(counts, text='Материалы: —', style='Success.TLabel')
        self.media_status.pack(side='left', padx=12)
        self.pattern = tk.StringVar(value='По смыслу рассказа')
        self.resolution = tk.StringVar(value='1280×720')
        self.fade = tk.StringVar(value='0')
        self.burn_subs = tk.BooleanVar(value=False)
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=(0, 4))
        ttk.Label(row, text='Формат найденного фото:', style='Muted.TLabel').pack(side='left', padx=(0, 8))
        self.layout = tk.StringVar(value='Авто: 16:9 или 9:16')
        self.combo(row, self.layout, list(LAYOUTS), 23).pack(side='left')
        self.effect = tk.StringVar(value='Появление снизу')
        self.names_on = tk.BooleanVar(value=True)
        check = ttk.Checkbutton(row, text='Имена при произнесении', variable=self.names_on)
        check.pack(side='left', padx=12)
        self.controls.append((check, 'normal'))
        self.button(row, 'Редактор имён и таймкодов', self.edit_names).pack(side='left')
        row = ttk.Frame(outer)
        row.pack(fill='x')
        ttk.Label(row, text='Появление фото:', style='Muted.TLabel').pack(side='left', padx=(0, 10))
        for (label, _), color in zip(EFFECTS.items(), ('Blue', 'Purple', 'Green', 'Peach', 'Teal', 'Muted')):
            choice = ttk.Radiobutton(row, text=label, variable=self.effect, value=label, style=color + '.TRadiobutton')
            choice.pack(side='left', padx=(0, 14))
            self.controls.append((choice, 'normal'))
        self.external = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(outer, text='Загружать веб / YouTube-материалы, которые я вправе использовать', variable=self.external)
        check.pack(anchor='w')
        self.controls.append((check, 'normal'))
        activity = ttk.Frame(outer)
        activity.pack(fill='x', pady=(6, 4))
        ttk.Label(activity, text='Сейчас:', style='Muted.TLabel').pack(side='left')
        self.status = ttk.Label(activity, text='Готов к работе', style='Activity.TLabel')
        self.status.pack(side='left', padx=10)
        self.stop_button = ttk.Button(activity, text='Остановить подбор', command=self.stop.set, state='disabled')
        self.stop_button.pack(side='right')
        self.stop_button.pack_forget()
        self.progress = ttk.Progressbar(outer, mode='indeterminate', maximum=100)
        self.progress.pack(fill='x', pady=(0, 12))
        self.log_box = tk.Text(outer, height=3, state='disabled', wrap='word', bg='#181825', fg=theme.GREEN,
                               insertbackground=theme.TEXT, relief='flat', padx=10, pady=8,
                               highlightthickness=1, highlightbackground=theme.PANEL)
        self.log_box.pack(side='bottom', fill='x', pady=(4, 0))
        self.log_box.tag_configure('error', foreground=theme.RED)
        self.log_box.tag_configure('info', foreground=theme.GREEN)
        ttk.Label(outer, text='Журнал работы', style='Heading.TLabel').pack(side='bottom', anchor='w', pady=(8, 4))
        body = ttk.Panedwindow(outer, orient='horizontal')
        body.pack(fill='both', expand=True)
        left, right = ttk.Frame(body, width=410), ttk.Frame(body, padding=(16, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=2)
        ttk.Label(left, text='Сцены рассказа', style='Heading.TLabel').pack(anchor='w', pady=(0, 8))
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=('time', 'kind', 'status', 'text'), show='headings', selectmode='browse')
        for key, text, width in [('time', 'Время', 115), ('kind', 'Источник', 100), ('status', 'Файл', 65), ('text', 'Озвучка', 225)]:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, stretch=key == 'text')
        self.tree.tag_configure('ready', foreground=theme.GREEN)
        self.tree.tag_configure('pending', foreground=theme.TEXT)
        scrollbar = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_scene)
        ttk.Label(right, text='Выбранная сцена', style='Heading.TLabel').pack(anchor='w', pady=(0, 8))
        self.scene_label = ttk.Label(right, text='Откройте озвучку и выберите сцену слева.', wraplength=740, style='Scene.TLabel')
        self.scene_label.pack(fill='x')
        self.file_label = ttk.Label(right, text='Материал не назначен', wraplength=740, style='Success.TLabel')
        self.file_label.pack(fill='x', pady=(8, 2))
        tabs = ttk.Notebook(right)
        tabs.pack(fill='both', expand=True, pady=(4, 0))
        variants = ttk.Frame(tabs, padding=(0, 6))
        clips = ttk.Frame(tabs, padding=10)
        tabs.add(variants, text='Варианты')
        tabs.add(clips, text='Клип и ссылки')
        row = ttk.Frame(variants)
        row.grid(row=0, column=0, sticky='ew', pady=5)
        variants.columnconfigure(0, weight=1)
        variants.rowconfigure(2, weight=1)
        self.kind = tk.StringVar(value='Фото')
        self.combo(row, self.kind, list(KINDS), 17).pack(side='left')
        self.query = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.query)
        entry.pack(side='left', fill='x', expand=True, padx=5)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Найти и добавить', self.search, 'Blue').pack(side='left')
        ttk.Label(variants, text='Лучший доступный вариант добавляется автоматически', style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=(5, 4))
        # The hidden tree keeps a stable selection model for keyboard/action callbacks.
        self.candidates = ttk.Treeview(right, columns=('provider', 'score', 'title'), show='headings', selectmode='browse')
        self.candidates.bind('<<TreeviewSelect>>', self.select_candidate)
        self.cards_canvas = tk.Canvas(variants, height=194, bg=theme.BG, highlightthickness=0)
        self.cards_canvas.grid(row=2, column=0, sticky='nsew')
        self.cards_frame = ttk.Frame(self.cards_canvas)
        self.cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_frame, anchor='nw')
        self.cards_frame.bind('<Configure>', lambda event: self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox('all')))
        scroll = ttk.Scrollbar(variants, orient='horizontal', command=self.cards_canvas.xview)
        scroll.grid(row=3, column=0, sticky='ew', pady=(0, 6))
        self.cards_canvas.configure(xscrollcommand=scroll.set)
        self.cards_canvas.bind('<Configure>', self.resize_cards)
        right.bind('<Configure>', lambda event: self.scene_label.configure(wraplength=max(260, event.width - 40)))
        self.preview = ttk.Label(variants)  # Compatibility with existing preview callbacks; cards show the images.
        row = ttk.Frame(variants)
        row.grid(row=4, column=0, sticky='ew', pady=3)
        self.action_buttons = [self.button(row, 'Назначить выбранное', self.assign_candidate, 'Green'),
            self.button(row, 'Открыть источник', self.open_source),
            self.button(row, 'Свой файл', self.local_file),
            self.button(row, 'Превью сцены', self.preview_scene, 'Purple')]
        for i, button in enumerate(self.action_buttons):
            button.grid(row=0, column=i, padx=(0, 5), pady=2, sticky='ew')
        row.bind('<Configure>', self.resize_actions)
        row = ttk.Frame(clips)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text='Начало клипа, сек:', style='Muted.TLabel').pack(side='left')
        self.source_start = tk.StringVar(value='0')
        entry = ttk.Entry(row, textvariable=self.source_start, width=8)
        entry.pack(side='left', padx=5)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Таймкод по субтитрам', self.find_start).pack(side='left')
        self.button(row, 'Сохранить начало', self.save_offset).pack(side='left', padx=5)
        self.match_label = ttk.Label(clips, text='', wraplength=740, style='Muted.TLabel')
        self.match_label.pack(fill='x')
        row = ttk.Frame(clips)
        row.pack(fill='x', pady=3)
        self.url = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.url)
        entry.pack(side='left', fill='x', expand=True)
        self.controls.append((entry, 'normal'))
        self.button(row, 'Добавить ссылку на видео', self.add_url).pack(side='left', padx=5)
        row = ttk.Frame(clips)
        row.pack(fill='x', pady=3)
        self.button(row, 'Снять назначение', self.clear_scene).pack(side='left')
        self.button(row, 'Открыть файл', self.play_file).pack(side='left', padx=5)
        self.button(row, 'Стиль сцены', self.scene_style, 'Peach').pack(side='left')
        export_settings = ttk.LabelFrame(clips, text='Настройки готового видео', padding=10)
        export_settings.pack(fill='x', pady=(14, 0))
        self.combo(export_settings, self.resolution, ['1280×720', '1920×1080', '720×1280'], 13).pack(side='left')
        ttk.Label(export_settings, text='Затемнение, сек:').pack(side='left', padx=(10, 3))
        entry = ttk.Entry(export_settings, textvariable=self.fade, width=4)
        entry.pack(side='left', padx=4)
        self.controls.append((entry, 'normal'))
        check = ttk.Checkbutton(export_settings, text='Субтитры', variable=self.burn_subs)
        check.pack(side='left', padx=8)
        self.controls.append((check, 'normal'))
        self.clear_cards()
        self.root.after(100, self.poll)

    def button(self, parent, text, command, color=None):
        def guarded():
            if self.busy:
                return
            try:
                command()
            except Exception as exc:
                self.message.showerror('Ошибка', str(exc))
        widget = self.ttk.Button(parent, text=text, command=guarded, width=0, style=color + '.TButton' if color else 'TButton')
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
                    tag = 'error' if any(t in payload.lower() for t in ('ошибк', 'http ', 'не удалось', 'не выдала')) else 'info'
                    self.log_box.insert('end', payload + '\n', tag)
                    self.log_box.see('end')
                    self.log_box.config(state='disabled')
                elif kind == 'done':
                    self.busy = False
                    self.progress.stop()
                    self.progress.configure(value=0)
                    for widget, state in self.controls:
                        widget.configure(state=state)
                    self.stop_button.configure(state='disabled')
                    self.stop_button.pack_forget()
                    self.refresh()
                    if payload:
                        payload()
                elif kind == 'error':
                    self.message.showerror('Операция не завершена', payload)
                elif kind == 'auto_progress':
                    ready, total = payload
                    self.media_status.configure(text=f'Материалы: {ready}/{total} назначено')
                    self.status.configure(text=f'Автоматически заполнено: {ready}/{total}')
                    self.progress.stop()
                    self.progress.configure(mode='determinate', maximum=max(1, total), value=ready)
                elif kind == 'render_phase':
                    self.stop_button.configure(state='disabled')
                    self.status.configure(text='Собираю видео…')
                    self.progress.configure(mode='indeterminate', maximum=100, value=0)
                    self.progress.start(15)
                elif kind == 'preview':
                    token, image = payload
                    if self.candidate and token == self.candidate['id']:
                        from PIL import ImageTk
                        self.preview_ref = ImageTk.PhotoImage(image)
                        self.preview.configure(image=self.preview_ref, text='')
                elif kind == 'card_image':
                    generation, index, image = payload
                    if generation == self.card_generation and index in self.card_widgets:
                        self.card_originals[index] = image
                        self.paint_card(index)
                        self.card_widgets[index]['image'].delete('placeholder')
                elif kind == 'card_unavailable':
                    generation, index = payload
                    if generation == self.card_generation and index in self.card_widgets:
                        self.card_widgets[index]['image'].itemconfigure('placeholder', text='Превью недоступно')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def task(self, label, function, done=None, stoppable=False):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.status.configure(text=label)
        self.progress.configure(mode='indeterminate', maximum=100, value=0)
        self.progress.start(15)
        for widget, _ in self.controls:
            widget.configure(state='disabled')
        if stoppable:
            self.stop_button.configure(state='normal')
            self.stop_button.pack(side='right')
        def work():
            success = False
            try:
                function()
                success = not (stoppable and self.stop.is_set())
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
        self.clear_cards()
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
            self.clear_cards()
        desired_ids = [str(i) for i in range(len(self.project.segments))]
        if list(self.tree.get_children()) != desired_ids:
            self.tree.delete(*self.tree.get_children())
        done = 0
        for i, seg in enumerate(self.project.segments):
            has_file = bool(seg.get('footage_path') and Path(seg['footage_path']).is_file())
            done += has_file
            values = (timecode(seg['start']), LABELS.get(seg.get('desired_kind'), '?'),
                      '✓' if has_file else '—', seg['text'])
            if self.tree.exists(str(i)):
                self.tree.item(str(i), values=values, tags=('ready' if has_file else 'pending',))
            else:
                self.tree.insert('', 'end', iid=str(i), values=values, tags=('ready' if has_file else 'pending',))
        self.status.configure(text=f'Назначено {done}/{len(self.project.segments)}')
        self.transcript_status.configure(text=f'Транскрипция: {len(self.project.segments)} сцен')
        self.media_status.configure(text=f'Материалы: {done}/{len(self.project.segments)} назначено')
        if self.selected is not None and self.selected < len(self.project.segments):
            if self.tree.selection() != (str(self.selected),):
                self.tree.selection_set(str(self.selected))
            seg = self.project.segments[self.selected]
            self.file_label.configure(text=f"Файл: {seg.get('footage_path') or 'не назначен'}")
        self.update_card_selection()

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
        self.scene_label.configure(text=f"Сцена {self.selected + 1} · {timecode(seg['start'])} → {timecode(seg['end'])}\n{seg['text']}")
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
        self.candidate = None
        self.clear_cards(empty=not seg.get('candidates'))
        generation = self.card_generation
        thumbnails = []
        for i, candidate in enumerate(seg.get('candidates', [])):
            score = candidate.get('relevance_score')
            self.candidates.insert('', 'end', iid=str(i), values=(candidate['provider'],
                f'{score:.0%}' if score is not None else '—', candidate.get('title') or candidate.get('query', '')))
            colors = self.colors
            cell = self.tk.Frame(self.cards_frame, bg=colors.CARD, highlightthickness=2,
                                 highlightbackground=colors.PANEL)
            cell.pack(side='left', padx=(0, 10), pady=2)
            image = self.tk.Canvas(cell, width=200, height=100, bg=colors.PANEL, highlightthickness=0, cursor='hand2')
            image.pack(padx=3, pady=3)
            image.create_text(100, 50, text='Загрузка превью…' if candidate.get('preview_image') else 'Без превью',
                              fill=colors.MUTED, tags='placeholder')
            provider = candidate['provider']
            color = {'youtube': colors.RED, 'pexels': colors.GREEN, 'pixabay': colors.TEAL,
                     'web': colors.PURPLE, 'webvideo': colors.PURPLE}.get(provider, colors.BLUE)
            badge = provider + (f' · смысл {score:.0%}' if score is not None else '')
            self.tk.Label(cell, text=badge, bg=colors.CARD, fg=color, font=('Arial', 10, 'bold')).pack(anchor='w', padx=6)
            title = candidate.get('title') or candidate.get('query') or 'Материал'
            title_label = self.tk.Label(cell, text=title[:74], bg=colors.CARD, fg=colors.TEXT, wraplength=195,
                          height=2, anchor='w', justify='left', font=('Arial', 10))
            title_label.pack(fill='x', padx=6)
            button = self.button(cell, 'Выбрать', lambda index=i: self.choose_card(index))
            button.configure(padding=(6, 3))
            button.pack(fill='x', padx=4, pady=(2, 4))
            image.bind('<Button-1>', lambda event, index=i: self.choose_card(index))
            self.card_widgets[i] = {'cell': cell, 'image': image, 'button': button, 'title': title_label}
            if candidate.get('preview_image'):
                thumbnails.append((i, candidate['preview_image']))
        self.update_card_selection()
        self.root.after_idle(self.resize_cards)
        if thumbnails:
            def download_all():
                from concurrent.futures import ThreadPoolExecutor
                def download(item):
                    if generation != self.card_generation:
                        return
                    index, url = item
                    try:
                        import io
                        from PIL import Image, ImageOps
                        with media.requests.get(media.safe_url(url), timeout=(5, 12), stream=True) as response:
                            response.raise_for_status()
                            data = bytearray()
                            for chunk in response.iter_content(65536):
                                data.extend(chunk)
                                if len(data) > 12 * 1024 * 1024:
                                    raise ValueError('Preview too large')
                        with Image.open(io.BytesIO(data)) as original:
                            thumbnail = ImageOps.pad(ImageOps.exif_transpose(original).convert('RGB'),
                                                     (200, 100), color=self.colors.PANEL)
                            self.events.put(('card_image', (generation, index, thumbnail)))
                    except Exception:
                        self.events.put(('card_unavailable', (generation, index)))
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(download, thumbnails))
            threading.Thread(target=download_all, daemon=True).start()

    def clear_cards(self, empty=True):
        self.card_generation += 1
        old_buttons = {c['button'] for c in self.card_widgets.values()}
        self.controls[:] = [(w, state) for w, state in self.controls if w not in old_buttons]
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
        self.card_images.clear()
        self.card_originals.clear()
        self.cards_canvas.xview_moveto(0)
        if empty:
            self.ttk.Label(self.cards_frame, text='Здесь появятся автоматически подобранные фото и видео.\nНажмите «Найти и добавить» или «Подобрать все».',
                           style='Muted.TLabel', padding=(20, 50)).pack()

    def choose_card(self, index):
        if not self.busy:
            self.candidates.selection_set(str(index))
            self.select_candidate()

    def resize_cards(self, _event=None):
        height = self.cards_canvas.winfo_height()
        compact = height < 180
        image_height = max(30, min(100, height - (48 if compact else 96)))
        for index, widgets in self.card_widgets.items():
            widgets['image'].configure(height=image_height)
            widgets['image'].coords('placeholder', 100, image_height / 2)
            widgets['title'].configure(height=1 if compact else 2)
            if compact:
                widgets['button'].pack_forget()
            else:
                widgets['button'].pack(fill='x', padx=4, pady=(2, 4))
            self.paint_card(index)

    def resize_toolbar(self, event):
        widths = [widget.winfo_reqwidth() + 6 for widget in self.toolbar_items]
        columns = next((n for n in (8, 4, 2) if max(sum(widths[i:i + n])
                            for i in range(0, len(widths), n)) <= event.width), 2)
        for i, widget in enumerate(self.toolbar_items):
            widget.grid(row=i // columns, column=i % columns)

    def resize_actions(self, event):
        widths = [button.winfo_reqwidth() + 5 for button in self.action_buttons]
        columns = 4 if sum(widths) <= event.width else 2 if max(sum(widths[:2]), sum(widths[2:])) <= event.width else 1
        for i, button in enumerate(self.action_buttons):
            button.grid(row=i // columns, column=i % columns)

    def paint_card(self, index):
        if index not in self.card_originals:
            return
        from PIL import ImageOps, ImageTk
        canvas = self.card_widgets[index]['image']
        height = int(canvas.cget('height'))
        thumbnail = ImageOps.pad(self.card_originals[index], (200, height), color=self.colors.PANEL)
        self.card_images[index] = ImageTk.PhotoImage(thumbnail)
        canvas.delete('thumbnail')
        canvas.create_image(100, height / 2, image=self.card_images[index], tags='thumbnail')

    def update_card_selection(self):
        assigned = self.require_scene().get('selected_candidate_id') if self.project and self.selected is not None else None
        for index, widgets in self.card_widgets.items():
            candidates = self.require_scene().get('candidates', [])
            if index >= len(candidates):
                continue
            candidate = candidates[index]
            chosen = self.candidate is candidate
            ready = candidate.get('id') == assigned
            widgets['cell'].configure(highlightbackground=self.colors.BLUE if chosen else self.colors.GREEN if ready else self.colors.PANEL)
            widgets['button'].configure(text='Выбрано' if chosen else 'Назначено ✓' if ready else 'Выбрать')

    def select_candidate(self, _event=None):
        if self.busy:
            return
        selection = self.candidates.selection()
        if not selection:
            return
        candidate = self.require_scene()['candidates'][int(selection[0])]
        if self.candidate is candidate:
            return
        self.candidate = candidate
        self.update_card_selection()
        self.source_start.set('0')

    def search(self):
        seg = self.require_scene()
        self.settings()
        project = self.project
        new_query = self.query.get().strip()
        if new_query != seg.get('search_query', ''):
            seg['manual_query'] = True
            seg['candidates'] = []
        if seg.get('desired_kind') != KINDS[self.kind.get()]:
            seg['candidates'] = []
        seg.update(desired_kind=KINDS[self.kind.get()], search_query=new_query)
        project.save()
        if seg['desired_kind'] == 'auto':
            self.auto_pick()
            return
        index = self.selected
        def work():
            media.auto_pick(project, self.log, self.stop.is_set, scene_indices=[index], replace_existing=True,
                           progress=self.auto_progress)
        self.task('Ищу и автоматически добавляю…', work, self.fill_candidates, stoppable=True)

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
        self.task('План и автоматический подбор…', lambda: plan_and_pick(project, self.log, self.stop.is_set, self.auto_progress),
                  done=self.select_scene, stoppable=True)

    def auto_progress(self, ready, total):
        self.events.put(('auto_progress', (ready, total)))

    def render(self):
        self.settings()
        project = self.project
        output = project.project_dir / 'final_video.mp4'
        def work():
            if any(not s.get('footage_path') or not Path(s['footage_path']).is_file() for s in project.segments):
                plan_and_pick(project, self.log, self.stop.is_set, self.auto_progress)
            if self.stop.is_set():
                return
            self.events.put(('render_phase', None))
            render.build_video(project.segments, project.audio_path, output,
                               project.settings, self.log, name_titles=project.name_titles)
        self.task('Подготавливаю и собираю…', work,
                  lambda: self.message.showinfo('Готово', f'Видео сохранено:\n{output}\n\nРядом: субтитры и список источников.'),
                  stoppable=True)

    def key_settings(self):
        from dotenv import dotenv_values, set_key
        path = Path(__file__).with_name('.env')
        values = dotenv_values(path) if path.exists() else {}
        win = self.tk.Toplevel(self.root)
        win.configure(bg=self.colors.BG)
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
        self.ttk.Label(frame, text='Текстовая модель').grid(row=3, column=0, sticky='w', pady=5)
        model = self.tk.StringVar(value=values.get('FASTGEN_TEXT_MODEL') or os.getenv('FASTGEN_TEXT_MODEL', ''))
        variables['FASTGEN_TEXT_MODEL'] = model
        models = self.ttk.Combobox(frame, textvariable=model, width=41)
        models.grid(row=3, column=1, padx=8, pady=5)
        model_result = {}
        def fetch_models():
            import fastgen_text
            key = variables['FASTGEN_API_KEY'].get().strip()
            if not key:
                self.message.showerror('Нет ключа', 'Введите ключ Fast-gen в поле выше.', parent=win)
                return
            def work():
                model_result['models'] = fastgen_text.available_models(api_key=key)
            def done():
                if win.winfo_exists():
                    models.configure(values=model_result['models'])
                    if not model.get():
                        model.set(model_result['models'][0])
                    models.focus_set()
            self.task('Получаю список текстовых моделей…', work, done)
        self.ttk.Button(frame, text='Получить список моделей', command=fetch_models).grid(row=4, column=1, sticky='w', padx=8)
        self.ttk.Label(frame, text='Пустая модель — автоматический выбор из доступных в вашем аккаунте.\n'
                       'Можно выбрать из списка или вписать ID текстовой модели Fast-gen.\n'
                       'Ключи хранятся локально; значения скрыты в журнале и GitHub.').grid(row=5, column=0, columnspan=2, pady=12)
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
        self.ttk.Button(frame, text='Сохранить', command=save, style='Green.TButton').grid(row=6, column=1, sticky='e')

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
            seg.update(photo_layout=LAYOUTS[layout.get()], photo_layout_manual=True, effect=EFFECTS[effect.get()])
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
            plan_and_pick(project)
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
