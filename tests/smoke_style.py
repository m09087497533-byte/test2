"""Видеопроверка нового стиля: три портрета 9:16, последовательное появление и имя."""
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from render import build_video, make_clip, probe


def frame(path, time):
    import io
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(time), '-i', str(path),
        '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
    return Image.open(io.BytesIO(data)).convert('RGB')


def main(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    photo = directory / 'synthetic-portrait.png'
    image = Image.new('RGB', (360, 640), '#b74430')
    draw = ImageDraw.Draw(image)
    draw.ellipse((90, 90, 270, 285), fill='#edba8a')
    draw.polygon([(150, 280), (210, 280), (320, 630), (40, 630)], fill='#4059a0')
    image.save(photo)
    audio = directory / 'style-audio.wav'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'sine=frequency=360:sample_rate=48000:duration=8', str(audio)], check=True)
    scene = {'start': 0, 'end': 8, 'text': 'Технический пример оформления.', 'footage_path': str(photo),
             'media_kind': 'photo', 'photo_layout': 'portrait_triptych', 'effect': 'slide_up'}
    titles = [{'name': 'Ольга Корбут', 'start': 2.65, 'end': 6.45, 'timing_quality': 'word'}]
    output = directory / 'SceneMix-style-preview.mp4'
    build_video([scene], audio, output, {'width': 640, 'height': 360, 'fps': 25,
                'transition': 0, 'subtitles': False, 'name_titles_enabled': True}, name_titles=titles)
    for time, present in [(0.30, [True, False, False]), (0.70, [True, True, False]), (1.2, [True, True, True])]:
        img = frame(output, time)
        for x, expected in zip((132, 320, 508), present):
            rgb = img.getpixel((x, 150))
            actual = not all(v > 235 for v in rgb)
            assert actual == expected, (time, x, rgb, expected)
    def dark_count(img):
        region = img.crop((170, 300, 470, 350))
        return sum(1 for r, g, b in zip(*[iter(region.tobytes())] * 3) if max(r, g, b) < 55)
    before, during, after = [dark_count(frame(output, t)) for t in (2.3, 3.2, 7.2)]
    assert during > before + 150 and during > after + 150, (before, during, after)
    assert next(s for s in probe(output)['streams'] if s['codec_type'] == 'video')['nb_frames'] == '200'
    print('PASS: последовательные три портрета, 9:16, титр включается/исчезает по времени, 8с без сдвига.')
    for effect in ('slide_left', 'pop', 'fade', 'slow_zoom', 'none'):
        clip = directory / f'effect-{effect}.mp4'
        item = {**scene, 'frames': 40, 'effect': effect}
        make_clip(item, clip, 640, 360, 25, 0, 0)
        assert next(s for s in probe(clip)['streams'] if s['codec_type'] == 'video')['nb_frames'] == '40'
    print('PASS: все эффекты появления отрендерены, точное число кадров сохранено.')
    # Длинное исходное действие ограничено восемью секундами, затем кадр удерживается.
    source = directory / 'motion12.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=s=320x180:r=25:d=12', '-c:v', 'libx264', '-threads', '2', str(source)], check=True)
    freeze = directory / 'motion8-held.mp4'
    make_clip({'footage_path': str(source), 'media_kind': 'video', 'frames': 275, 'source_start': 1.0},
              freeze, 640, 360, 25, 0, 0)
    a, b, c = (frame(freeze, t) for t in (2, 8.4, 10.5))
    from PIL import ImageChops, ImageStat
    same = sum(ImageStat.Stat(ImageChops.difference(b, c)).mean)
    moving = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)
    assert same < 3 and moving > 12, (same, moving)
    assert next(s for s in probe(freeze)['streams'] if s['codec_type'] == 'video')['nb_frames'] == '275'
    print('PASS: источник обрезан до 8с, последующий кадр удерживается, зацикливания нет.')
    frame(output, 3.2).save(directory / 'style-frame.png')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/scenemix-style')
