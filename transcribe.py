"""
Транскрибация аудио через fast-gen.ai (aistudio_gemini_3.5_transcribe).
Возвращает сегменты речи с таймкодами — по ним потом подбираются футажи.
"""

import os
import re
import ssl
import time
import json
import base64
import mimetypes
import urllib.request
from pathlib import Path


FASTGEN_API_KEY = os.getenv("FASTGEN_API_KEY")
FASTGEN_BASE = "https://api.fast-gen.ai"
TRANSCRIBE_OPERATION = "aistudio_gemini_3.5_transcribe"

MAX_INLINE_AUDIO_BYTES = 25 * 1024 * 1024  # лимит fast-gen.ai для inline-аудио (data URI)


def _http(method: str, path: str, body: dict = None) -> dict:
    try:
        FASTGEN_API_KEY.encode("ascii")
    except UnicodeEncodeError:
        raise RuntimeError(
            "FASTGEN_API_KEY в .env выглядит как незаменённый плейсхолдер "
            "(содержит не-английские символы) — открой .env и впиши туда настоящий ключ."
        )
    headers = {"X-API-Key": FASTGEN_API_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{FASTGEN_BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _audio_to_data_uri(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_INLINE_AUDIO_BYTES:
        raise ValueError(
            f"Файл {path.name} весит {size / 1024 / 1024:.1f} МБ — "
            f"больше лимита в 25 МБ для аудио. Пережми в mp3 с битрейтом поменьше "
            f"(например: ffmpeg -i {path.name} -b:a 96k out.mp3)."
        )
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "audio/mpeg"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


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
    fast-gen.ai иногда отдаёт один сегмент на целый абзац без пауз.
    Режем такие сегменты на сцены поменьше, чтобы на каждую можно было
    подобрать свой футаж, а не один на весь текст.
    """
    scenes = []
    for seg in segments:
        scenes.extend(_split_segment_into_scenes(seg, max_duration, min_duration))
    return scenes


def transcribe_audio(audio_path: Path, language_codes=None, progress_cb=None) -> dict:
    """
    Возвращает:
    {
        "text": "весь текст одним куском",
        "segments": [
            {
                "text": "...", "start": 0.0, "end": 3.2, "speaker": None,
                "words": [{"text": "...", "start": 0.0, "end": 0.4}, ...]
            },
            ...
        ]
    }
    """
    if not FASTGEN_API_KEY:
        raise RuntimeError("Нет FASTGEN_API_KEY в .env")

    def report(msg):
        if progress_cb:
            progress_cb(msg)

    report("Готовлю аудио...")
    data_uri = _audio_to_data_uri(Path(audio_path))

    report("Отправляю на транскрибацию...")
    body = {
        "operation": TRANSCRIBE_OPERATION,
        "inputs": [data_uri],
        "word_timestamps": True,
        "diarization": False,
        "smart": False,
    }
    if language_codes:
        body["language_codes"] = language_codes

    resp = _http("POST", "/api/v6/generations", body)
    gen_id = resp["id"]

    for _ in range(150):  # до 5 минут ожидания
        time.sleep(2)
        status = _http("GET", f"/api/v6/generations/{gen_id}")
        s = status["status"]
        report(f"Статус: {s}...")
        if s == "succeeded":
            if not status.get("results"):
                raise RuntimeError("Транскрибация вернула пустой результат")
            result = status["results"][0]
            transcription = result.get("transcription") or {}
            segments_raw = transcription.get("segments") or []
            segments = [
                {
                    "text": seg["text"].strip(),
                    "start": seg.get("start_seconds") or 0.0,
                    "end": seg.get("end_seconds") or 0.0,
                    "speaker": seg.get("speaker"),
                    "words": [
                        {
                            "text": w["text"],
                            "start": w.get("start_seconds"),
                            "end": w.get("end_seconds"),
                        }
                        for w in (seg.get("words") or [])
                    ],
                }
                for seg in segments_raw
                if seg.get("text", "").strip()
            ]
            scenes = split_into_scenes(segments)
            report(f"Разбито на {len(scenes)} сцен (было {len(segments)} сегмент(ов) от API)")
            return {"text": result.get("text") or "", "segments": scenes}
        elif s == "failed":
            raise RuntimeError(f"Транскрибация не удалась: {status.get('error')}")

    raise TimeoutError("Транскрибация: таймаут")
