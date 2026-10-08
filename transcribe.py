"""Local Whisper transcription with word timestamps; audio stays on the laptop."""
import os
import re
import threading
from pathlib import Path
from project import normalize_segment_timing

_models = {}
_lock = threading.Lock()


def _load_model(name, report):
    from faster_whisper import WhisperModel
    cache = os.getenv('SCENEMIX_MODEL_CACHE') or str(Path.home() / '.cache' / 'scenemix' / 'whisper')
    key = (name, cache)
    with _lock:
        if key not in _models:
            report(f'Загружаю Whisper {name}. При первом запуске модель скачивается один раз.')
            try:
                _models[key] = WhisperModel(name, device='cpu', compute_type='int8', download_root=cache,
                                           cpu_threads=max(1, min(6, os.cpu_count() or 2)))
            except Exception:
                raise RuntimeError('Не удалось загрузить локальную модель Whisper. '
                    'Проверьте интернет для первого скачивания или путь к модели в настройках. '
                    'Можно продолжить с импортом SRT/VTT.') from None
        return _models[key]


_SENTENCE_END_RE = re.compile(r"[.!?…]+$")


def _split_segment_into_scenes(seg: dict, max_duration: float, min_duration: float) -> list[dict]:
    """
    Режет один (возможно длинный) сегмент на куски поменьше — по концам предложений,
    с принудительным разрезом, если предложение всё равно длиннее max_duration.
    """
    words = seg.get("words") or []
    if not words or any(w.get("start") is None or w.get("end") is None for w in words):
        # нет пословных таймкодов — разбить точно не можем, оставляем как есть
        return [seg]

    scenes = []
    current = []

    def flush():
        if not current:
            return
        text = " ".join(w["text"] for w in current).strip()
        if text:
            scenes.append({
                "text": text,
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "speaker": seg.get("speaker"),
                "words": list(current),
            })
        current.clear()

    for w in words:
        current.append(w)
        duration_so_far = current[-1]["end"] - current[0]["start"]
        is_sentence_end = bool(_SENTENCE_END_RE.search(w["text"]))
        if is_sentence_end and duration_so_far >= min_duration:
            flush()
        elif duration_so_far >= max_duration:
            flush()
    flush()

    return scenes if scenes else [seg]


def split_into_scenes(segments: list[dict], max_duration: float = 8.0, min_duration: float = 2.5) -> list[dict]:
    """
    Распознавание иногда отдаёт один сегмент на целый абзац без пауз.
    Режем такие сегменты на сцены поменьше, чтобы на каждую можно было
    подобрать свой футаж, а не один на весь текст.
    """
    scenes = []
    segments, _ = normalize_segment_timing(segments)
    for seg in segments:
        if seg.get('words') and all(w.get('start') is not None and w.get('end') is not None for w in seg['words']):
            from planner import timed_words
            seg['words'] = timed_words([seg])
        scenes.extend(_split_segment_into_scenes(seg, max_duration, min_duration))
    return normalize_segment_timing(scenes)[0]


def transcribe_audio(audio_path, language_codes=None, progress_cb=None, split_scenes=True, cancelled=lambda: False):
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError('Аудиофайл не найден.')
    report = progress_cb or (lambda _: None)
    if cancelled():
        raise RuntimeError('Распознавание остановлено.')
    model_name = os.getenv('WHISPER_MODEL', 'small').strip() or 'small'
    language = (language_codes[0] if language_codes else os.getenv('WHISPER_LANGUAGE', 'ru')).strip()
    model = _load_model(model_name, report)
    if cancelled():
        raise RuntimeError('Распознавание остановлено.')
    report('Распознаю аудио на ноутбуке; пословные таймкоды включены…')
    stream, info = model.transcribe(str(path), language=language or None, beam_size=5,
                                    word_timestamps=True, vad_filter=True, condition_on_previous_text=True)
    segments = []
    for item in stream:
        if cancelled():
            raise RuntimeError('Распознавание остановлено; прежний проект сохранён.')
        text = item.text.strip()
        if not text:
            continue
        words = [{'text': w.word.strip(), 'start': float(w.start), 'end': float(w.end)}
                 for w in (item.words or []) if w.word.strip()]
        segments.append({'text': text, 'start': float(item.start), 'end': float(item.end),
                         'words': words, 'timing_quality': 'word' if words else 'estimated'})
        total = float(getattr(info, 'duration', 0) or 0)
        report(f'Распознано: {min(100, round(item.end / total * 100)) if total else 0}% · {len(segments)} реплик')
    if not segments:
        raise ValueError('Whisper не обнаружил речь в аудио.')
    segments, repairs = normalize_segment_timing(segments)
    if repairs:
        report(f'Пересечения границ исправлены автоматически: {repairs}.')
    scenes = split_into_scenes(segments) if split_scenes else segments
    return {'text': ' '.join(s['text'] for s in scenes), 'segments': scenes, 'language': getattr(info, 'language', language)}
