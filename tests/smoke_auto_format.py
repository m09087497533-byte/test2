"""Render landscape and portrait photos in the same automatic-format project."""
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from render import build_video, make_clip, probe, resolve_photo_layout
from smoke_style import frame


def main(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    wide = directory / 'wide16x9.png'
    vertical = directory / 'portrait9x16.png'
    image = Image.new('RGB', (640, 360), '#b74430')
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 628, 348), outline='#edd58a', width=12)
    image.save(wide)
    Image.new('RGB', (360, 640), '#35a24b').save(vertical)
    rotated = directory / 'rotated.jpg'
    image = Image.new('RGB', (640, 360), '#b74430')
    ImageDraw.Draw(image).rectangle((320, 0, 640, 360), fill='#3535a2')
    exif = image.getexif(); exif[274] = 6
    image.save(rotated, exif=exif)
    settings = {'photo_layout': 'auto', 'width': 640, 'height': 360, 'fps': 25, 'transition': 0}
    assert resolve_photo_layout({'footage_path': str(wide), 'photo_layout': 'portrait_triptych'}, settings) == 'full_bleed'
    assert resolve_photo_layout({'footage_path': str(vertical)}, settings) == 'portrait_triptych'
    assert resolve_photo_layout({'footage_path': str(rotated)}, settings) == 'portrait_triptych'
    assert resolve_photo_layout({'footage_path': str(vertical), 'photo_layout': 'full_bleed', 'photo_layout_manual': True}, settings) == 'full_bleed'
    audio = directory / 'audio.wav'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=8', str(audio)], check=True)
    scenes = [{'start': 0, 'end': 4, 'text': 'Горизонтальная фотография.', 'footage_path': str(wide),
               'media_kind': 'photo', 'photo_layout': 'portrait_triptych', 'effect': 'none'},
              {'start': 4, 'end': 8, 'text': 'Вертикальная фотография.', 'footage_path': str(vertical),
               'media_kind': 'photo', 'effect': 'none'}]
    output = directory / 'SceneMix-auto-formats.mp4'
    build_video(scenes, audio, output, settings)
    landscape_frame, portrait_frame = frame(output, 2), frame(output, 6)
    # Landscape fills the frame; orientation is chosen from the file, ignoring old AI style hints.
    assert landscape_frame.getpixel((2, 2))[0] > 140
    assert max(landscape_frame.getpixel((2, 2))[1:]) < 120
    # The portrait appears as three green 9:16 cards on a white background.
    assert min(portrait_frame.getpixel((2, 2))) > 235
    for x in (132, 320, 508):
        r, g, b = portrait_frame.getpixel((x, 150))
        assert g > r + 50 and g > b + 50
    video = next(s for s in probe(output)['streams'] if s['codec_type'] == 'video')
    assert video['nb_frames'] == '200'
    rotated_output = directory / 'exif-portrait.mp4'
    make_clip({'footage_path': str(rotated), 'media_kind': 'photo', 'effect': 'none', 'frames': 25},
              rotated_output, 640, 360, 25, 0, 0, settings)
    image = frame(rotated_output, 0.5)
    assert image.getpixel((320, 100))[0] > 140  # Top of the correctly rotated portrait is red.
    assert image.getpixel((320, 270))[2] > 140  # Bottom is blue, not a sideways crop.
    for effect in ('slide_up', 'slide_left', 'fade', 'pop', 'slow_zoom', 'none'):
        clip = directory / f'wide-{effect}.mp4'
        make_clip({'footage_path': str(wide), 'media_kind': 'photo', 'effect': effect, 'frames': 25},
                  clip, 640, 360, 25, 0, 0, settings)
        assert next(s for s in probe(clip)['streams'] if s['codec_type'] == 'video')['nb_frames'] == '25'
    print('PASS: auto 16:9/9:16 in one video, exact timing, rendered EXIF rotation, manual override, six wide-photo effects.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/scenemix-auto')
