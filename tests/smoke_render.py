"""Реальный смешанный рендер и проверка кадров/аудио через FFmpeg."""
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from project import Project
from render import build_video, probe, duration


def main(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    audio = output / 'demo.wav'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000:duration=5', str(audio)], check=True)
    for name, color in [('first.jpg', '#c83b30'), ('last.jpg', '#35a24b')]:
        image = Image.new('RGB', (640, 360), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 80, 490, 280), outline='white', width=8)
        image.save(output / name)
    stock = output / 'test_stock.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=640x360:r=25:d=0.6', '-c:v', 'libx264', '-threads', '2',
                    '-pix_fmt', 'yuv420p', str(stock)], check=True)
    project = Project(audio)
    project.set_segments_from_transcript([
        {'start': 0.4, 'end': 1.3, 'text': 'Первая сцена: фотография.'},
        {'start': 2, 'end': 2.6, 'text': 'Вторая сцена: видеоклип.'},
        {'start': 3.7, 'end': 4.6, 'text': 'Третья сцена: фотография.'},
    ])
    for i, name in enumerate(('first.jpg', 'test_stock.mp4', 'last.jpg')):
        project.assign(i, output / name)
    project.settings.update(width=640, height=360, subtitles=True, transition=0.1)
    project.save()
    final = build_video(project.segments, audio, project.project_dir / 'final_video.mp4', project.settings)
    info = probe(final)
    assert {s['codec_type'] for s in info['streams']} == {'video', 'audio'}
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    assert (video['width'], video['height']) == (640, 360)
    assert abs(duration(final) - 5) < 0.1
    # Проверяем порядок медиа и паузы по реальным кадрам, а не коду.
    for time, color in [(0.6, 0), (1.7, 0), (2.3, 2), (3.2, 2), (4, 1), (4.8, 1)]:
        data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(time), '-i', str(final),
                   '-frames:v', '1', '-vf', 'crop=40:40:20:20,scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        assert len(data) == 3 and data[color] > max(data[c] for c in range(3) if c != color) + 30, (time, list(data))
    # Видеоклип короче сцены: второй синий кадр подтверждает зацикливание.
    assert final.with_suffix('.srt').is_file()
    assert len(json.loads(final.with_suffix('.sources.json').read_text())) == 3
    print('PASS: фото → видео → фото, паузы, зацикливание, 5с аудио, субтитры, список источников.')
    # Проверяем повторный запуск и вертикальный формат без прожига субтитров.
    project.settings.update(width=360, height=640, subtitles=False, transition=0)
    vertical = build_video(project.segments, audio, project.project_dir / 'vertical.mp4', project.settings)
    v = next(s for s in probe(vertical)['streams'] if s['codec_type'] == 'video')
    assert (v['width'], v['height']) == (360, 640)
    assert abs(duration(vertical) - 5) < 0.1
    print('PASS: повторный рендер, вертикальное видео 360×640, озвучка без обрезки.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/scenemix-demo')
