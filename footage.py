"""
Поиск футажей на Pexels + Pixabay по смыслу сегмента транскрипта.
Для каждого сегмента сначала спрашиваем LLM fast-gen.ai (prompt_generation),
какую сцену показать на экране — и уже это короткое английское описание
используем как поисковый запрос.
"""

import os
import re
import json
import ssl
import socket

# страховка от зависаний: у deep_translator (и других сторонних либ) нет
# своего таймаута, а без этого сеть может повиснуть навсегда
socket.setdefaulttimeout(30)
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


FASTGEN_API_KEY = os.getenv("FASTGEN_API_KEY")
FASTGEN_BASE = "https://api.fast-gen.ai"

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PEXELS_BASE = "https://api.pexels.com/videos/search"

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
PIXABAY_BASE = "https://pixabay.com/api/videos/"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

try:
    from deep_translator import GoogleTranslator
    _HAS_TRANSLATOR = True
except ImportError:
    _HAS_TRANSLATOR = False


def _fastgen_post(path: str, body: dict) -> dict:
    headers = {"X-API-Key": FASTGEN_API_KEY, "Content-Type": "application/json", **_UA}
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{FASTGEN_BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _fallback_translate_query(text: str) -> str:
    """Запасной вариант, если LLM недоступен: перевод + первые слова."""
    query = text
    if _HAS_TRANSLATOR:
        try:
            query = GoogleTranslator(source="auto", target="en").translate(text)
        except Exception:
            query = text
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", query)
    return " ".join(words[:6]) or "abstract background"


def generate_scene_query(segment_text: str) -> str:
    """
    Просит LLM fast-gen.ai описать, какую сцену показать на экране под этот
    кусок озвучки — возвращает короткий поисковый запрос на английском.
    """
    segment_text = (segment_text or "").strip()
    if not segment_text:
        return "abstract background"

    if not FASTGEN_API_KEY:
        return _fallback_translate_query(segment_text)

    prompt = (
        "You are choosing stock video footage to illustrate a voiceover narration. "
        "Read the narration segment below and decide the single best visual scene "
        "to show on screen while it is being spoken. Think about what it actually "
        "depicts — not a literal word-by-word translation — and pay close attention to "
        "any historical period, setting, weather, time of day, and mood implied by "
        "the text (e.g. era-appropriate clothing, architecture, objects, technology "
        "if a time period is implied; rural vs urban; indoor vs outdoor; season).\n\n"
        f"Narration segment (may be in Russian): \"{segment_text}\"\n\n"
        "Reply with ONLY an English stock-footage search query: 6 to 12 concrete "
        "words describing the scene in detail (subject, action, setting, era/period "
        "if relevant, weather/lighting, mood). No quotes, no punctuation, "
        "no explanation — just the query itself."
    )

    try:
        resp = _fastgen_post("/api/v6/prompts/generate", {"user_prompt": prompt})
        generated = (resp.get("generated_text") or "").strip()
        words = re.findall(r"[A-Za-z0-9]+", generated)
        short = " ".join(words[:12])
        return short or _fallback_translate_query(segment_text)
    except Exception:
        return _fallback_translate_query(segment_text)


def _search_pexels(query: str, per_page: int) -> list[dict]:
    if not PEXELS_API_KEY:
        return []
    params = urllib.parse.urlencode({
        "query": query,
        "per_page": per_page,
        "orientation": "landscape",
    })
    req = urllib.request.Request(
        f"{PEXELS_BASE}?{params}",
        headers={"Authorization": PEXELS_API_KEY, **_UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    results = []
    for video in data.get("videos", []):
        files = [f for f in video.get("video_files", []) if f.get("link")]
        if not files:
            continue
        # берём файл ближе к HD (1280 шириной) — не тащим лишний вес 4K
        hd_files = sorted(
            [f for f in files if (f.get("width") or 0) >= 1280],
            key=lambda f: f.get("width") or 0,
        )
        best = hd_files[0] if hd_files else max(files, key=lambda f: f.get("width") or 0)
        results.append({
            "id": f"pexels_{video['id']}",
            "title": (video.get("url") or "Pexels video").rstrip("/").rsplit("/", 1)[-1],
            "provider": "pexels",
            "source_url": video.get("url"),
            "license_url": "https://www.pexels.com/license/",
            "query": query,
            "preview_image": video.get("image"),
            "video_url": best["link"],
            "width": best.get("width"),
            "height": best.get("height"),
            "duration": video.get("duration"),
        })
    return results


def _search_pixabay(query: str, per_page: int) -> list[dict]:
    if not PIXABAY_API_KEY:
        return []
    params = urllib.parse.urlencode({
        "key": PIXABAY_API_KEY,
        "q": query,
        "per_page": max(per_page, 3),  # у Pixabay минимум 3
        "video_type": "film",
        "orientation": "horizontal",
        "safesearch": "true",
    })
    req = urllib.request.Request(f"{PIXABAY_BASE}?{params}", headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    results = []
    for video in data.get("hits", [])[:per_page]:
        variants = video.get("videos", {})
        # предпочитаем medium (обычно ~960px) — компромисс качество/вес
        best = variants.get("medium") or variants.get("large") or variants.get("small") or variants.get("tiny")
        if not best or not best.get("url"):
            continue
        picture_id = video.get("picture_id")
        preview_image = (
            best.get("thumbnail")
            or (f"https://i.vimeocdn.com/video/{picture_id}_200x150.jpg" if picture_id else None)
        )
        results.append({
            "id": f"pixabay_{video['id']}",
            "title": video.get("tags", "Pixabay video"),
            "provider": "pixabay",
            "source_url": video.get("pageURL"),
            "license_url": "https://pixabay.com/service/license-summary/",
            "query": query,
            "preview_image": preview_image,
            "video_url": best["url"],
            "width": best.get("width"),
            "height": best.get("height"),
            "duration": video.get("duration"),
        })
    return results


def search_footage(segment_text: str, per_page: int = 6) -> tuple[list[dict], list[str]]:
    """
    Ищет одновременно на Pexels и Pixabay (какие ключи есть в .env — те и используются).
    Если один из сервисов вернул ошибку — не роняет весь поиск, а возвращает
    предупреждение по нему и продолжает с тем, что получилось от второго.

    Возвращает: (candidates, warnings)
    """
    if not PEXELS_API_KEY and not PIXABAY_API_KEY:
        raise RuntimeError("Нет ни PEXELS_API_KEY, ни PIXABAY_API_KEY в .env — добавь хотя бы один")

    query = generate_scene_query(segment_text)
    results = []
    warnings = []

    if PEXELS_API_KEY:
        try:
            results += _search_pexels(query, per_page)
        except urllib.error.HTTPError as e:
            warnings.append(f"Pexels: HTTP {e.code} — проверь PEXELS_API_KEY в .env ({e.reason})")
        except Exception as e:
            warnings.append(f"Pexels: {e}")

    if PIXABAY_API_KEY:
        try:
            results += _search_pixabay(query, per_page)
        except urllib.error.HTTPError as e:
            warnings.append(f"Pixabay: HTTP {e.code} — проверь PIXABAY_API_KEY в .env ({e.reason})")
        except Exception as e:
            warnings.append(f"Pixabay: {e}")

    return results, warnings


def _download_bytes(url: str, dest_path: Path) -> Path:
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
    return dest_path, content_type


def _verify_video(path: Path) -> bool:
    """Проверяет через ffprobe, что файл реально видео с ненулевой длительностью."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        return result.returncode == 0 and duration > 0 and any(
            s.get("codec_type") == "video" for s in data.get("streams", []))
    except Exception:
        return False


def download_footage(url: str, dest_path: Path) -> Path:
    """Скачивает видео и проверяет, что это реально рабочий видеофайл (не битая ссылка/HTML-ошибка)."""
    dest_path = Path(dest_path)
    path, content_type = _download_bytes(url, dest_path)

    if content_type and not content_type.startswith("video/") and "octet-stream" not in content_type:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"Сервер вернул не видео ({content_type or 'неизвестный тип'}) — ссылка нерабочая")

    if not _verify_video(path):
        path.unlink(missing_ok=True)
        raise RuntimeError("Скачанный файл повреждён — ffprobe не смог его прочитать")

    return path


def download_preview(url: str, dest_path: Path) -> Path:
    path, _ = _download_bytes(url, dest_path)
    return path
