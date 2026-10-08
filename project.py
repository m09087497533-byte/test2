"""Единый проект: совместим с footage_path из обоих исходных редакторов."""
import copy
import datetime
import json
import math
import os
from pathlib import Path

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
SOURCE_MODES = {'auto', 'photo', 'stock', 'youtube', 'web'}


def requested_source(settings, scene):
    return scene.get('source_override') or (settings.get('source_mode') if settings.get('source_mode') != 'auto' else None)


def effective_source(settings, scene):
    return requested_source(settings, scene) or scene.get('desired_kind', 'auto')


def source_matches(settings, scene, candidate=None):
    required = requested_source(settings, scene)
    if not required:
        return True
    item = candidate or scene
    provider = item.get('provider', item.get('footage_source', 'local'))
    kind = item.get('media_kind') or media_kind(item.get('footage_path', ''))
    return ((required == 'photo' and kind == 'photo') or
            (required == 'stock' and kind == 'video' and provider in ('pexels', 'pixabay', 'local')) or
            (required == 'youtube' and provider == 'youtube') or
            (required == 'web' and kind == 'video' and provider == 'webvideo'))


def scene_ready(settings, scene):
    path = scene.get('footage_path')
    return bool(path and Path(path).is_file() and source_matches(settings, scene) and
                (not settings.get('visual_verification', True) or scene.get('visual_verified') or
                 scene.get('footage_source', 'local') == 'local' or scene.get('review_status') == 'manually_selected'))
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


def normalize_segment_timing(segments):
    """Repair overlap on a copy, keeping absolute time, text and media assignments.

    Distinct starts are never moved: the earlier shot ends at the next start.
    Equal starts share their available interval. Gaps and the final end remain
    intact; invalid or backwards input is rejected before anything is saved.
    Word timestamps are separate from shot boundaries and are not modified here.
    """
    result = copy.deepcopy(segments)
    if not result:
        validate_segments(result)
    for i, seg in enumerate(result):
        start, end = float(seg['start']), float(seg['end'])
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
            raise ValueError(f'Некорректный таймкод сцены {i + 1}')
        if i and start < result[i - 1]['start']:
            raise ValueError(f'Таймкоды сцены {i + 1} идут назад: неверный порядок исходных данных.')
        seg.update(start=start, end=end)
    i = 0
    while i < len(result):
        j = i + 1
        start = result[i]['start']
        while j < len(result) and result[j]['start'] == start:
            j += 1
        if j - i > 1:
            end = max(s['end'] for s in result[i:j])
            if j < len(result):
                end = min(end, result[j]['start'])
            weights = [max(1, len(str(s.get('text', '')).split())) for s in result[i:j]]
            step, used = (end - start) / sum(weights), 0
            for k, weight in zip(range(i, j), weights):
                result[k]['start'] = start + step * used
                used += weight
                result[k]['end'] = end if k == j - 1 else start + step * used
        i = j
    repaired = 0
    for i, seg in enumerate(result):
        if i + 1 < len(result):
            seg['end'] = min(seg['end'], result[i + 1]['start'])
        if (seg['start'], seg['end']) != (float(segments[i]['start']), float(segments[i]['end'])):
            seg['timing_repaired'] = True
            repaired += 1
    validate_segments(result)
    return result, repaired


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
        self.timing_repair_count = 0
        self.settings = {
            'pattern': 'По смыслу рассказа', 'width': 1280, 'height': 720, 'fps': 25,
            'transition': 0, 'subtitles': False, 'allow_external': False,
            'photo_layout': 'auto', 'photo_effect': 'slide_up',
            'name_titles_enabled': True, 'min_scene': 6.0, 'photo_max': 14.0, 'video_max': 8.0,
            'source_mode': 'auto', 'visual_verification': True,
        }

    def ensure_dirs(self):
        for path in (self.project_dir, self.media_dir, self.thumbs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def set_segments_from_transcript(self, segments):
        segments, self.timing_repair_count = normalize_segment_timing(segments)
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
            if seg.get('timing_repaired'):
                item['timing_repaired'] = True
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
            visual_verified=candidate.get('visual_verified', False),
            review_status='automatically_selected' if candidate.get('visual_verified') else 'manually_selected',
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
        segments, repairs = normalize_segment_timing(data['segments']) if data.get('segments') else ([], 0)
        raw, raw_repairs = normalize_segment_timing(data['raw_transcript']) if data.get('raw_transcript') else ([], 0)
        self.settings.update(data.get('settings', {}))
        if data.get('version', 0) < 4 and self.settings['photo_layout'] == 'portrait_triptych':
            self.settings['photo_layout'] = 'auto'
        self.segments = segments
        self.story = data.get('story', {})
        self.name_titles = data.get('name_titles', [])
        self.raw_transcript = raw
        self.timing_repair_count = repairs + raw_repairs
        pattern = PATTERNS.get(self.settings['pattern'], ['photo', 'stock'])
        for i, seg in enumerate(self.segments):
            seg['index'] = i
            seg.setdefault('desired_kind', pattern[i % len(pattern)])
            seg.setdefault('source_start', 0.0)
            if seg.get('footage_path'):
                seg.setdefault('media_kind', media_kind(seg['footage_path']))
        if self.timing_repair_count:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
            backup = self.project_dir / f'project.before-timing.{stamp}.json'
            backup.write_bytes(self.project_file.read_bytes())
            self.save()
        return True
