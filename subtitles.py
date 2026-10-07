"""SRT/VTT без API: сохраняем таймкоды и паузы, делим длинные реплики."""
import html
import re
from pathlib import Path
from project import validate_segments

TIME = re.compile(r'(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})')


def seconds(value):
    match = TIME.fullmatch(value.strip())
    if not match:
        raise ValueError('Некорректный таймкод: ' + value)
    h, m, s, ms = match.groups()
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def import_subtitle_file(path, max_duration=6.0):
    segments = []
    raw = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
    for block in re.split(r'\n\s*\n', raw):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if '-->' not in line:
                continue
            left, right = line.split('-->', 1)
            start, end = seconds(left), seconds(right.strip().split()[0])
            text = html.unescape(re.sub(r'<[^>]*>|\{[^}]*\}', '', ' '.join(lines[i + 1:]))).strip()
            if not text:
                break
            if end <= start:
                raise ValueError('Конец реплики должен быть позже начала')
            words = text.split()
            count = min(len(words), max(1, __import__('math').ceil((end - start) / max_duration)))
            for j in range(count):
                lo, hi = j * len(words) // count, (j + 1) * len(words) // count
                segments.append({'text': ' '.join(words[lo:hi]),
                                 'start': start + (end - start) * lo / len(words),
                                 'end': start + (end - start) * hi / len(words)})
            break
    validate_segments(segments)
    return {'text': ' '.join(s['text'] for s in segments), 'segments': segments}


def timestamp(value):
    ms = round(value * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def write_srt(segments, path):
    lines = []
    for i, seg in enumerate(segments):
        # Пользовательский текст не должен внедрять теги в фильтр субтитров.
        text = str(seg['text']).replace('\n', ' ').replace('\r', ' ')
        text = re.sub(r'<[^>]*>|\{[^}]*\}', '', text)
        lines.append(f"{i + 1}\n{timestamp(seg['start'])} --> {timestamp(seg['end'])}\n{text}\n")
    Path(path).write_text('\n'.join(lines), encoding='utf-8')
