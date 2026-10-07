"""Смешанный монтаж FFmpeg; тайминг привязан к аудио, без накопления округлений."""
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from project import media_kind, normalize_segment_timing
from subtitles import write_srt
from titles import write_name_ass


def resolve_photo_layout(item, settings):
    # The actual downloaded image decides the format; search metadata can be wrong.
    layout = settings.get('photo_layout', 'auto')
    if item.get('photo_layout_manual'):
        layout = item.get('photo_layout') or layout
    elif 'photo_layout' not in settings:
        layout = item.get('photo_layout') or layout
    if layout == 'auto':
        from PIL import Image, ImageOps
        with Image.open(item['footage_path']) as image:
            w, h = ImageOps.exif_transpose(image).size
        return 'portrait_triptych' if h > w else 'full_bleed'
    return layout


def photo_card_graph(width, height, fps, effect='slide_up', triptych=False):
    """9:16 на белом поле; три копии — композиция внутри одной сцены, не смена сюжетов."""
    count = 3 if triptych and width > height else 1
    gap = max(4, round(width * 0.009))
    card_h = int(min(height * 0.90, (width - gap * (count + 1)) / count * 16 / 9)) // 2 * 2
    card_w = int(card_h * 9 / 16) // 2 * 2
    offset = (width - count * card_w - (count - 1) * gap) / 2
    top = (height - card_h) / 2
    graph = [f'[1:v]fps={fps},split={count}' + ''.join(f'[p{i}]' for i in range(count))]
    previous = '0:v'
    for i in range(count):
        start = i * 0.4 if count == 3 else 0
        x = offset + i * (card_w + gap)
        y = str(top)
        if effect == 'slide_up':
            y = f'{top}+{height * 0.07}*max(0,1-(t-{start})/0.35)'
        elif effect == 'slide_left':
            x = f'{x}+{width * 0.08}*max(0,1-(t-{start})/0.35)'
        base = (f'scale={card_w}:{card_h}:force_original_aspect_ratio=increase,'
                f'crop={card_w}:{card_h}:x=(iw-ow)/2:y=(ih-oh)*0.30,setsar=1,format=rgba')
        if effect in ('pop', 'slow_zoom'):
            zoom = (f'0.92+0.08*min(1,max(0,(t-{start})/0.35))' if effect == 'pop'
                    else '1+0.008*t')
            base += f",scale=w='trunc({card_w}*({zoom})/2)*2':h='trunc({card_h}*({zoom})/2)*2':eval=frame"
            x = f'{offset + i * (card_w + gap)}+({card_w}-overlay_w)/2'
            y = f'{top}+({card_h}-overlay_h)/2'
        if effect != 'none':
            base += f',fade=t=in:st={start}:d=0.20:alpha=1'
        graph.append(f'[p{i}]{base}[c{i}]')
        graph.append(f"[{previous}][c{i}]overlay=x='{x}':y='{y}':enable='gte(t,{start})':"
                     f'eof_action=repeat:shortest=1[o{i}]')
        previous = f'o{i}'
    graph.append(f'[{previous}]format=yuv420p,setsar=1,setpts=PTS-STARTPTS[out]')
    return ';'.join(graph)


def wide_photo_graph(width, height, fps, effect='slide_up'):
    """Landscape photos keep their aspect ratio and use the selected appearance effect."""
    filters = (f'fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,'
               'setsar=1,format=rgba')
    x, y = '(main_w-overlay_w)/2', '(main_h-overlay_h)/2'
    if effect == 'slide_up':
        y += f'+{height * 0.07}*max(0,1-t/0.35)'
    elif effect == 'slide_left':
        x += f'+{width * 0.08}*max(0,1-t/0.35)'
    elif effect in ('pop', 'slow_zoom'):
        zoom = '0.92+0.08*min(1,t/0.35)' if effect == 'pop' else '1+0.008*t'
        filters += f",scale=w='trunc(iw*({zoom})/2)*2':h='trunc(ih*({zoom})/2)*2':eval=frame"
    if effect != 'none':
        filters += ',fade=t=in:st=0:d=0.20:alpha=1'
    return (f'[1:v]{filters}[photo];[0:v][photo]overlay=x=\'{x}\':y=\'{y}\':'
            'eof_action=repeat:shortest=1,format=yuv420p,setsar=1,setpts=PTS-STARTPTS[out]')


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
    segments, _ = normalize_segment_timing(segments)
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


def make_clip(item, out, width, height, fps, transition, index, settings=None):
    settings = settings or {}
    frames = item['frames']
    length = frames / fps
    path = Path(item['footage_path']).resolve()
    kind = item.get('media_kind') or media_kind(path)
    if kind == 'photo':
        layout = resolve_photo_layout(item, settings)
        from PIL import Image, ImageOps
        with Image.open(path) as original:
            if original.getexif().get(274, 1) != 1:
                # FFmpeg does not consistently honour JPEG EXIF orientation.
                oriented = ImageOps.exif_transpose(original)
                if oriented.mode not in ('RGB', 'RGBA'):
                    oriented = oriented.convert('RGB')
                path = out.with_name(out.stem + '_oriented.png')
                oriented.save(path)
        effect = item.get('effect') or settings.get('photo_effect', 'slide_up')
        if layout in ('portrait_card', 'portrait_triptych'):
            graph = photo_card_graph(width, height, fps, effect, layout == 'portrait_triptych')
        else:
            graph = wide_photo_graph(width, height, fps, effect)
        fade = min(transition, length / 3)
        if fade > 0:
            graph = graph.replace('[out]', f'[photo_out];[photo_out]fade=t=in:st=0:d={fade},'
                                  f'fade=t=out:st={length - fade}:d={fade}[out]')
        run(['ffmpeg', '-y', '-v', 'error', '-filter_complex_threads', '1',
             '-f', 'lavfi', '-i', f'color=white:s={width}x{height}:r={fps}:d={length}',
             '-loop', '1', '-framerate', str(fps), '-i', str(path),
             '-filter_complex', graph, '-map', '[out]', '-frames:v', str(frames), '-an',
             '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20',
             '-pix_fmt', 'yuv420p', '-r', str(fps), str(out)], timeout=max(120, length * 20))
        return
    else:
        start = float(item.get('source_start', 0))
        source_duration = duration(path)
        if not math.isfinite(start) or start < 0 or start >= source_duration:
            raise ValueError(f'Начало фрагмента вне видео: сцена {index + 1}')
        limit = min(8.0, float(settings.get('video_max', 8.0)), source_duration - start)
        if start > 0 or source_duration > limit + 0.03:
            # Отдельный конечный файл: внутренний trim после -ss в FFmpeg может обрезать кадры tpad.
            cut = out.with_name(out.stem + '_source.mp4')
            seek = ['-ss', str(start)] if start > 0 else []
            run(['ffmpeg', '-y', '-v', 'error', *seek, '-i', str(path), '-t', str(limit),
                 '-an', '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast',
                 '-pix_fmt', 'yuv420p', str(cut)], timeout=max(120, limit * 20))
            path = cut
        inputs = ['-i', str(path)]
        # Не зацикливаем спортивное действие. После <=8с движения держим последний кадр на паузе.
        vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
              f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
              f'tpad=stop_mode=clone:stop_duration={length},fps={fps}')
    fade = min(transition, length / 3)
    if fade > 0:
        vf += f',fade=t=in:st=0:d={fade},fade=t=out:st={length - fade}:d={fade}'
    vf += ',format=yuv420p'
    run(['ffmpeg', '-y', '-v', 'error', '-filter_threads', '1', *inputs,
         '-map', '0:v:0', '-vf', vf, '-frames:v', str(frames), '-an',
         '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20',
         '-pix_fmt', 'yuv420p', '-r', str(fps), str(out)], timeout=max(120, length * 20))


def build_video(segments, audio_path, output_path, settings=None, log_fn=print, name_titles=None):
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
    segments, repairs = normalize_segment_timing(segments)
    if repairs:
        log_fn(f'Пересечения сцен исправлены автоматически: {repairs}.')
    items = timeline(segments, audio_duration, fps)
    missing = [i + 1 for i, s in enumerate(items) if not s.get('footage_path') or not Path(s['footage_path']).is_file()]
    if missing:
        raise ValueError('Источники пока не дали доступные файлы для сцен: ' + ', '.join(map(str, missing)) +
                         '. Проверьте подключение и запустите повторный автоподбор.')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mixed_render_', dir=output_path.parent) as tmp:
        tmp = Path(tmp)
        clips = []
        for i, item in enumerate(items):
            out = tmp / f'clip_{i:05}.mp4'
            log_fn(f'Монтаж сцены {i + 1}/{len(items)}: {item.get("media_kind") or media_kind(item["footage_path"])}')
            make_clip(item, out, width, height, fps, transition, i, settings)
            stream = next(s for s in probe(out)['streams'] if s['codec_type'] == 'video')
            if int(stream.get('nb_frames', -1)) != item['frames']:
                raise RuntimeError(f'Длительность сцены {i + 1} не совпадает с планом; сборка остановлена.')
            clips.append(out)
        (tmp / 'concat.txt').write_text(''.join(f"file '{p.name}'\n" for p in clips), encoding='utf-8')
        video_only = tmp / 'video.mp4'
        run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', 'concat.txt',
             '-c', 'copy', str(video_only)], cwd=tmp, timeout=max(180, audio_duration * 2))
        result = tmp / 'final.mp4'
        command = ['ffmpeg', '-y', '-v', 'error', '-filter_threads', '1',
                   '-i', str(video_only), '-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0']
        filters = []
        if settings.get('subtitles', False):
            write_srt(segments, tmp / 'subtitles.srt')
            filters.append('subtitles=subtitles.srt:force_style=\'FontSize=22,Outline=2,MarginV=28\'')
        if name_titles and settings.get('name_titles_enabled', True):
            write_name_ass(name_titles, tmp / 'names.ass', width, height, audio_duration)
            filters.append('ass=names.ass')
        if filters:
            command += ['-vf', ','.join(filters),
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
    output_path.with_suffix('.names.json').write_text(json.dumps(name_titles or [], ensure_ascii=False, indent=2), encoding='utf-8')
    log_fn(f'Готово: {output_path}')
    return output_path
