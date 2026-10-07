"""Именные титры ASS: реальные абсолютные таймкоды, без зависимости от границ сцен."""
import math
from pathlib import Path


def ass_time(seconds):
    cs = max(0, round(float(seconds) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f'{h}:{m:02}:{s:02}.{cs:02}'


def clean_name(name):
    return str(name).replace('\\', '').replace('{', '').replace('}', '').replace('\n', ' ').replace('\r', ' ').strip()


def write_name_ass(titles, path, width, height, audio_duration):
    font_size = max(18, round(height * 0.056))
    margin = max(18, round(height * 0.05))
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Name,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1.5,2,30,30,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    lines = []
    for title in sorted(titles, key=lambda t: float(t['start'])):
        start, end = float(title['start']), min(audio_duration, float(title['end']))
        name = clean_name(title['name'])
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
            raise ValueError('Неверный таймкод именного титра')
        if name:
            lines.append(f'Dialogue: 1,{ass_time(start)},{ass_time(end)},Name,,0,0,0,,{{\\fad(160,200)}}{name}\n')
    Path(path).write_text(header + ''.join(lines), encoding='utf-8')
    return len(lines)
