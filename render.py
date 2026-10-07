"""Смешанный монтаж FFmpeg; тайминг привязан к аудио, без накопления округлений."""
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from project import media_kind, validate_segments
from subtitles import write_srt


def run(command, cwd=None, timeout=600):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('FFmpeg: ' + result.stderr[-1800:])
    return result.stdout


def probe(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                           '-of', 'json', str(Path(path).resolve())], timeout=30))
    return data


def duration(path):
    value = float(probe(path)['format'].get('duration', 0))
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Файл имеет неизвестную или нулевую длительность')
    return value


def timeline(segments, audio_duration, fps):
    validate_segments(segments)
    if segments[-1]['end'] > audio_duration + 0.15:
        raise ValueError('Субтитры выходят за конец аудио. Проверьте, что выбрана правильная озвучка.')
    total_frames = math.ceil(audio_duration * fps)
    result = []
    for i, seg in enumerate(segments):
        first = 0 if i == 0 else round(seg['start'] * fps)
        last = round(segments[i + 1]['start'] * fps) if i + 1 < len(segments) else total_frames
        if first >= total_frames or last <= first:
            raise ValueError(f'Сцена {i + 1} слишком короткая или за пределами аудио')
        result.append({**seg, 'frames': last - first, 'timeline_start': first / fps,
                       'timeline_end': last / fps})
    return result


def make_clip(item, out, width, height, fps, transition, index):
    frames = item['frames']
    length = frames / fps
    path = Path(item['footage_path']).resolve()
    kind = item.get('media_kind') or media_kind(path)
    base = f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1'
    if kind == 'photo':
        # Один входной кадр -> строго нужное число кадров zoompan.
        progress = f'on/{max(1, frames - 1)}'
        zoom = f'1+0.10*{progress}' if index % 2 == 0 else f'1.10-0.10*{progress}'
        vf = (f'scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,'
              f'crop={width * 2}:{height * 2},'
              f"zoompan=z='{zoom}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':"
              f'd={frames}:s={width}x{height}:fps={fps},setsar=1')
        inputs = ['-i', str(path)]
    else:
        start = float(item.get('source_start', 0))
        if not math.isfinite(start) or start < 0 or start >= duration(path):
            raise ValueError(f'Начало фрагмента вне видео: сцена {index + 1}')
        inputs = ['-stream_loop', '-1', '-ss', str(start), '-i', str(path)]
        vf = base + f',fps={fps}'
    fade = min(transition, length / 3)
    if fade > 0:
        vf += f',fade=t=in:st=0:d={fade},fade=t=out:st={length - fade}:d={fade}'
    vf += ',format=yuv420p,setpts=PTS-STARTPTS'
    run(['ffmpeg', '-y', '-v', 'error', '-filter_threads', '1', *inputs,
         '-map', '0:v:0', '-vf', vf, '-frames:v', str(frames), '-an',
         '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20',
         '-pix_fmt', 'yuv420p', '-r', str(fps), str(out)], timeout=max(120, length * 20))


def build_video(segments, audio_path, output_path, settings=None, log_fn=print):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('Установите FFmpeg и добавьте ffmpeg/ffprobe в PATH')
    settings = settings or {}
    width, height, fps = (int(settings.get('width', 1280)), int(settings.get('height', 720)),
                          int(settings.get('fps', 25)))
    if width < 2 or height < 2 or width % 2 or height % 2 or fps < 1 or fps > 60:
        raise ValueError('Нужны чётные размеры кадра и FPS от 1 до 60')
    transition = float(settings.get('transition', 0.15))
    if not math.isfinite(transition) or not 0 <= transition <= 2:
        raise ValueError('Длительность затемнения должна быть от 0 до 2 секунд')
    audio_path, output_path = Path(audio_path).resolve(), Path(output_path).resolve()
    if output_path == audio_path:
        raise ValueError('Выходной файл не может совпадать с исходным аудио')
    audio_info = probe(audio_path)
    if not any(s['codec_type'] == 'audio' for s in audio_info['streams']):
        raise ValueError('В выбранном файле нет аудиодорожки')
    audio_duration = duration(audio_path)
    items = timeline(segments, audio_duration, fps)
    missing = [i + 1 for i, s in enumerate(items) if not s.get('footage_path') or not Path(s['footage_path']).is_file()]
    if missing:
        raise ValueError('Назначьте файлы для сцен: ' + ', '.join(map(str, missing)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mixed_render_', dir=output_path.parent) as tmp:
        tmp = Path(tmp)
        clips = []
        for i, item in enumerate(items):
            out = tmp / f'clip_{i:05}.mp4'
            log_fn(f'Монтаж сцены {i + 1}/{len(items)}: {item.get("media_kind") or media_kind(item["footage_path"])}')
            make_clip(item, out, width, height, fps, transition, i)
            clips.append(out)
        (tmp / 'concat.txt').write_text(''.join(f"file '{p.name}'\n" for p in clips), encoding='utf-8')
        video_only = tmp / 'video.mp4'
        run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', 'concat.txt',
             '-c', 'copy', str(video_only)], cwd=tmp, timeout=max(180, audio_duration * 2))
        result = tmp / 'final.mp4'
        command = ['ffmpeg', '-y', '-v', 'error', '-filter_threads', '1',
                   '-i', str(video_only), '-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0']
        if settings.get('subtitles', True):
            write_srt(segments, tmp / 'subtitles.srt')
            command += ['-vf', 'subtitles=subtitles.srt:force_style=\'FontSize=22,Outline=2,MarginV=28\'',
                        '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20']
        else:
            command += ['-c:v', 'copy']
        command += ['-c:a', 'aac', '-b:a', '192k', '-t', str(audio_duration),
                    '-movflags', '+faststart', str(result)]
        log_fn('Добавляю озвучку и субтитры…')
        run(command, cwd=tmp, timeout=max(180, audio_duration * 20))
        info = probe(result)
        types = {s['codec_type'] for s in info['streams']}
        if not {'audio', 'video'} <= types or abs(duration(result) - audio_duration) > 0.15:
            raise RuntimeError('Финальная проверка дорожек/длительности не пройдена')
        # Не оставляем частично записанный final_video.mp4 при неудачном рендере.
        os.replace(result, output_path)
    write_srt(segments, output_path.with_suffix('.srt'))
    sources = [{'scene': i + 1, 'start': s['start'], 'end': s['end'],
                'provider': s.get('footage_source', 'local'), 'url': s.get('source_url', ''),
                'license_url': s.get('license_url', ''), 'query': s.get('footage_query', ''),
                'original_source_start': s.get('original_source_start', s.get('source_start', 0))}
               for i, s in enumerate(segments)]
    output_path.with_suffix('.sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    log_fn(f'Готово: {output_path}')
    return output_path
