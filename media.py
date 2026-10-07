"""Общий поиск и загрузка фото, стоков и фрагментов YouTube/веб-видео."""
import hashlib
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
from ddgs import DDGS
import photo
import footage


def safe_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Нужна ссылка HTTP/HTTPS без встроенных учётных данных')
    return url


def stable_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def run_ytdlp(args, timeout=90):
    import sys
    result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--no-playlist',
                             '--socket-timeout', '20', '--retries', '1',
                             '--no-warnings', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Не выводим URL подписанных потоков или данные прокси из вывода загрузчика.
        raise RuntimeError('YouTube/сайт недоступен для yt-dlp. Возможны ограничения доступа; '
                           'попробуйте другой источник или свой файл.')
    return result.stdout


def semantic_query(seg, context=''):
    manual = seg.get('search_query', '').strip()
    if manual:
        return manual
    text = seg['text']
    if context:
        text = f'Context: {context[:700]}\nScene to illustrate: {text}'
    return photo.generate_scene_query(text)


def search(seg, kind=None, context='', limit=6):
    kind = kind or seg.get('desired_kind', 'photo')
    query = semantic_query(seg, context)
    warnings = []
    if not photo.FASTGEN_API_KEY and not seg.get('search_query'):
        warnings.append('FASTGEN_API_KEY не задан: используется перевод текста вместо LLM-планирования.')
    results = []
    if kind == 'photo':
        results, skipped = photo._search_web_images(query, limit)
        if skipped:
            warnings.append(f'Отфильтровано фото с водяными знаками: {skipped}')
        for item in results:
            item['media_kind'] = 'photo'
    elif kind == 'stock':
        providers = [(footage.PEXELS_API_KEY, footage._search_pexels, 'Pexels'),
                     (footage.PIXABAY_API_KEY, footage._search_pixabay, 'Pixabay')]
        if not any(p[0] for p in providers):
            raise RuntimeError('Для стоков добавьте PEXELS_API_KEY или PIXABAY_API_KEY в .env')
        for key, function, name in providers:
            if not key:
                continue
            try:
                results.extend(function(query, limit))
            except Exception as exc:
                warnings.append(f'{name}: поиск недоступен ({type(exc).__name__}). Проверьте ключ и сеть.')
        for item in results:
            item['media_kind'] = 'video'
    elif kind == 'youtube':
        data = json.loads(run_ytdlp(['--flat-playlist', '--dump-single-json',
                                    f'ytsearch{limit}:{query}']))
        for entry in data.get('entries') or []:
            url = entry.get('webpage_url') or entry.get('url') or ''
            if not url.startswith('http'):
                url = 'https://www.youtube.com/watch?v=' + entry['id']
            thumbs = entry.get('thumbnails') or []
            results.append({'id': 'youtube_' + stable_id(url), 'provider': 'youtube',
                            'source_url': url, 'video_url': url, 'media_kind': 'video',
                            'title': entry.get('title', ''), 'duration': entry.get('duration'),
                            'preview_image': thumbs[-1]['url'] if thumbs else None})
    elif kind == 'web':
        with DDGS(timeout=20) as ddgs:
            for item in ddgs.videos(query, max_results=limit):
                url = item.get('content') or item.get('url')
                if not url:
                    continue
                safe_url(url)
                results.append({'id': 'webvideo_' + stable_id(url), 'provider': 'webvideo',
                                'source_url': url, 'video_url': url, 'media_kind': 'video',
                                'title': item.get('title', ''),
                                'preview_image': item.get('images', {}).get('medium')
                                    if isinstance(item.get('images'), dict) else None})
        warnings.append('Веб-поиск возвращает страницы: скачать можно только сайты, поддерживаемые yt-dlp.')
    else:
        raise ValueError('Неизвестный источник: ' + str(kind))
    for item in results:
        item['query'] = query
    if not results:
        warnings.append('Варианты не найдены. Измените запрос или назначьте свой файл.')
    return results, warnings


def rank_caption_window(events, text, duration, query=''):
    """Выбираем окно по словам narration/query. Это текстовый поиск, не анализ кадров."""
    stop = {'the', 'and', 'with', 'from', 'this', 'that', 'video', 'footage',
            'как', 'что', 'это', 'для', 'или', 'его', 'она', 'они', 'был', 'было'}
    tokens = lambda s: {w for w in re.findall(r'[\w]+', s.lower()) if len(w) > 2 and w not in stop}
    wanted = tokens(text + ' ' + query)
    best = None
    for i, cue in enumerate(events):
        start = cue['start']
        window = []
        for other in events[i:]:
            if other['start'] >= start + duration:
                break
            window.append(other['text'])
        excerpt = ' '.join(window)
        overlap = wanted & tokens(excerpt)
        score = len(overlap) / max(1, len(wanted))
        if overlap and (best is None or score > best['score']):
            best = {'start': start, 'score': score, 'excerpt': excerpt[:450]}
    return best


def suggest_start(candidate, seg):
    url = safe_url(candidate['source_url'])
    info = json.loads(run_ytdlp(['--skip-download', '--dump-single-json', url]))
    tracks = info.get('subtitles') or info.get('automatic_captions') or {}
    languages = [name for prefix in ('ru', 'en') for name in tracks if name.startswith(prefix)]
    for language in dict.fromkeys(languages):
        track = next((t for t in tracks[language] if t.get('ext') == 'json3'), None)
        if not track:
            continue
        response = requests.get(safe_url(track['url']), timeout=25)
        response.raise_for_status()
        events = []
        for e in response.json().get('events', []):
            text = ''.join(t.get('utf8', '') for t in e.get('segs', [])).strip()
            if text:
                events.append({'start': e.get('tStartMs', 0) / 1000, 'text': text})
        result = rank_caption_window(events, seg['text'], seg['end'] - seg['start'], candidate.get('query', ''))
        if result:
            return result
    return None


def download(candidate, directory, duration, source_start=0.0, allow_external=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source_start, duration = float(source_start), float(duration)
    if not all(math.isfinite(x) for x in (source_start, duration)) or source_start < 0 or duration <= 0:
        raise ValueError('Некорректные границы фрагмента')
    url = safe_url(candidate.get('photo_url') or candidate['video_url'])
    key = stable_id(url + f'|{source_start:.3f}|{duration:.3f}')
    if candidate.get('media_kind') == 'photo':
        return photo.download_footage(url, directory / (key + '.jpg'))
    if candidate['provider'] in ('pexels', 'pixabay'):
        return footage.download_footage(url, directory / (key + '.mp4'))
    if not allow_external:
        raise ValueError('Для загрузки веб/YouTube-фрагментов подтвердите право использовать выбранные материалы.')
    dest = directory / (key + '.mp4')
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        template = str(Path(tmp) / 'source.%(ext)s')
        run_ytdlp(['-f', 'bv*[height<=1080]+ba/b[height<=1080]/best',
                   '--download-sections', f'*{source_start:.3f}-{source_start + duration:.3f}',
                   '--force-keyframes-at-cuts', '--merge-output-format', 'mp4',
                   '--output', template, url], timeout=max(180, duration * 30))
        files = [p for p in Path(tmp).glob('source.*') if p.suffix not in ('.part', '.ytdl')]
        source = next((p for p in files if footage._verify_video(p)), None)
        if source is None:
            raise RuntimeError('Не удалось получить проверенный видеофрагмент')
        # Унифицируем контейнер и кодек; звук исходного ролика не нужен.
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-t', str(duration),
                        '-an', '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p',
                        str(dest)], check=True, timeout=max(120, duration * 10))
    if not footage._verify_video(dest):
        dest.unlink(missing_ok=True)
        raise RuntimeError('Загруженный фрагмент повреждён')
    return dest


def auto_pick(project, log=print, cancelled=lambda: False):
    assigned = 0
    for i, seg in enumerate(project.segments):
        if cancelled():
            log('Остановлено. Готовые сцены сохранены.')
            break
        if seg.get('footage_path') and Path(seg['footage_path']).is_file():
            continue
        kind = seg.get('desired_kind', 'photo')
        if kind in ('youtube', 'web') and not project.settings['allow_external']:
            log(f'Сцена {i + 1}: загрузка внешних видео не включена.')
            continue
        context = ' '.join(s['text'] for s in project.segments[max(0, i - 1):i + 2])
        try:
            candidates, warnings = search(seg, context=context)
            seg['candidates'] = candidates
            project.save()
            for warning in warnings:
                log(warning)
            used = {s.get('selected_candidate_id') for s in project.segments if s.get('footage_path')}
            candidates = [c for c in candidates if c['id'] not in used] + [c for c in candidates if c['id'] in used]
            for candidate in candidates[:5]:
                if cancelled():
                    break
                try:
                    start = 0.0
                    if kind in ('youtube', 'web'):
                        try:
                            match = suggest_start(candidate, seg)
                            if match:
                                start = match['start']
                                candidate['caption_match'] = match
                                log(f"Сцена {i + 1}: совпадение в субтитрах на {start:.1f}с")
                            else:
                                log(f'Сцена {i + 1}: совпадений в субтитрах нет; беру начало найденного ролика. Проверьте сцену.')
                        except Exception:
                            log(f'Сцена {i + 1}: субтитры недоступны; беру начало найденного ролика. Проверьте сцену.')
                    path = download(candidate, project.media_dir, seg['end'] - seg['start'],
                                    start, project.settings['allow_external'])
                    project.assign(i, path, candidate, 0.0)
                    seg['original_source_start'] = start
                    project.save()
                    assigned += 1
                    log(f'Сцена {i + 1}/{len(project.segments)}: {candidate["provider"]}, сохранено.')
                    break
                except Exception as exc:
                    log(f'Вариант не загрузился ({type(exc).__name__}); пробую следующий.')
            else:
                log(f'Сцена {i + 1}: подходящий файл не загружен.')
        except Exception as exc:
            log(f'Сцена {i + 1}: {exc}')
    return assigned
