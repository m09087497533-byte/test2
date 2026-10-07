"""Единый проект: совместим с footage_path из обоих исходных редакторов."""
import json
import math
import os
from pathlib import Path

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
PATTERNS = {
    'По смыслу рассказа': ['auto'],
    'Фото / стоки': ['photo', 'stock'],
    'Фото / стоки / YouTube': ['photo', 'stock', 'photo', 'youtube'],
    'Фото / стоки / интернет': ['photo', 'stock', 'photo', 'web'],
    'Только фото': ['photo'],
    'Только стоки': ['stock'],
}


def media_kind(path):
    return 'photo' if Path(path).suffix.lower() in IMAGE_EXTENSIONS else 'video'


def validate_segments(segments):
    if not segments:
        raise ValueError('Нет сцен. Транскрибируйте аудио или импортируйте SRT/VTT.')
    previous_end = 0.0
    for i, seg in enumerate(segments):
        start, end = float(seg['start']), float(seg['end'])
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
            raise ValueError(f'Некорректный таймкод сцены {i + 1}')
        if start < previous_end - 0.001:
            raise ValueError(f'Сцена {i + 1} перекрывает предыдущую. Исправьте таймкоды.')
        previous_end = end


class Project:
    def __init__(self, audio_path):
        self.audio_path = Path(audio_path).resolve()
        self.project_dir = self.audio_path.parent / f'{self.audio_path.stem}_project'
        self.media_dir = self.project_dir / 'media'
        self.thumbs_dir = self.project_dir / 'thumbs'
        self.project_file = self.project_dir / 'project.json'
        self.segments = []
        self.story = {}
        self.name_titles = []
        self.raw_transcript = []
        self.settings = {
            'pattern': 'По смыслу рассказа', 'width': 1280, 'height': 720, 'fps': 25,
            'transition': 0, 'subtitles': False, 'allow_external': False,
            'photo_layout': 'auto', 'photo_effect': 'slide_up',
            'name_titles_enabled': True, 'min_scene': 6.0, 'photo_max': 14.0, 'video_max': 8.0,
        }

    def ensure_dirs(self):
        for path in (self.project_dir, self.media_dir, self.thumbs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def set_segments_from_transcript(self, segments):
        validate_segments(segments)
        old = {}
        for seg in self.segments:
            old.setdefault(seg['text'], []).append(seg)
        result = []
        pattern = PATTERNS[self.settings['pattern']]
        for i, seg in enumerate(segments):
            bucket = old.get(seg['text'], [])
            carried = bucket.pop(0) if bucket else {}
            item = dict(carried)
            item.update(index=i, text=seg['text'], start=float(seg['start']), end=float(seg['end']),
                        words=seg.get('words', []), timing_quality=seg.get('timing_quality', 'estimated'))
            item.setdefault('desired_kind', pattern[i % len(pattern)])
            item.setdefault('source_start', 0.0)
            item.setdefault('candidates', [])
            result.append(item)
        self.segments = result
        return sum(bool(s.get('footage_path')) for s in result)

    def apply_pattern(self, name):
        if name not in PATTERNS:
            raise ValueError('Неизвестная схема чередования')
        self.settings['pattern'] = name
        for i, seg in enumerate(self.segments):
            seg['desired_kind'] = PATTERNS[name][i % len(PATTERNS[name])]
        # Уже выбранные файлы сохраняются; их можно очистить отдельно.

    def assign(self, index, path, candidate=None, source_start=0.0):
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        start = float(source_start)
        if not math.isfinite(start) or start < 0:
            raise ValueError('Начало фрагмента должно быть >= 0')
        candidate = candidate or {}
        self.segments[index].pop('photo_fingerprint', None)
        self.segments[index].update(
            footage_path=str(path), media_kind=media_kind(path), source_start=start,
            footage_source=candidate.get('provider', 'local'),
            footage_query=candidate.get('query', ''),
            source_url=candidate.get('source_url', ''),
            license_url=candidate.get('license_url', ''),
            title=candidate.get('title', path.name),
            selected_candidate_id=candidate.get('id'),
            asset_url=candidate.get('photo_url') or candidate.get('video_url') or '',
        )
        self.save()

    def save(self):
        self.ensure_dirs()
        data = {'version': 4, 'audio_path': str(self.audio_path),
                'segments': self.segments, 'settings': self.settings}
        data.update(story=self.story, name_titles=self.name_titles, raw_transcript=self.raw_transcript)
        tmp = self.project_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.project_file)

    def load(self):
        if not self.project_file.exists():
            return False
        data = json.loads(self.project_file.read_text(encoding='utf-8'))
        self.settings.update(data.get('settings', {}))
        if data.get('version', 0) < 4 and self.settings['photo_layout'] == 'portrait_triptych':
            self.settings['photo_layout'] = 'auto'
        self.segments = data.get('segments', [])
        self.story = data.get('story', {})
        self.name_titles = data.get('name_titles', [])
        self.raw_transcript = data.get('raw_transcript', [])
        pattern = PATTERNS.get(self.settings['pattern'], ['photo', 'stock'])
        for i, seg in enumerate(self.segments):
            seg['index'] = i
            seg.setdefault('desired_kind', pattern[i % len(pattern)])
            seg.setdefault('source_start', 0.0)
            if seg.get('footage_path'):
                seg.setdefault('media_kind', media_kind(seg['footage_path']))
        return True
