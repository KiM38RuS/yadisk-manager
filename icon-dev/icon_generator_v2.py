#!/usr/bin/env python3
"""
YaDisk Manager — Flying Saucer Icon Generator v2
══════════════════════════════════════════════════════
Пропорции исправлены: широкий плоский корпус + маленький купол.
Луч засасывает файлы.
"""

from PIL import Image, ImageDraw, ImageFilter
import math, os, struct, shutil

# ── Палитры ──────────────────────────────────
THEMES = {
    'dark': {
        'body':         (175, 190, 205),
        'body_shadow':  (140, 155, 172),
        'body_rim':     (120, 135, 150),
        'dome':         (100, 190, 240),
        'dome_glint':   (180, 225, 255),
        'dome_rim':     (60, 140, 195),
        'beam_inner':   (180, 240, 255, 55),
        'beam_outer':   (100, 200, 255, 18),
        'beam_ring':    (220, 248, 255, 130),
        'file_body':    (255, 220, 80),
        'file_line':    (180, 150, 45),
        'file_tab':     (220, 180, 50),
    },
    'light': {
        'body':         (85, 100, 115),
        'body_shadow':  (60, 75, 90),
        'body_rim':     (45, 55, 70),
        'dome':         (50, 150, 210),
        'dome_glint':   (90, 185, 245),
        'dome_rim':     (30, 110, 170),
        'beam_inner':   (60, 210, 255, 50),
        'beam_outer':   (40, 170, 240, 15),
        'beam_ring':    (140, 225, 255, 110),
        'file_body':    (220, 185, 50),
        'file_line':    (155, 125, 30),
        'file_tab':     (185, 150, 35),
    }
}


def _draw_beam(draw, s, C, detail):
    """Рисует центрированный луч под тарелкой."""
    if detail < 2:
        return
    cx = s // 2
    top_y = int(s * 0.42)
    bot_y = s + 4

    top_w = int(s * 0.22)
    bot_w = int(s * 0.50)

    # Внешний
    draw.polygon([
        (cx - top_w, top_y),
        (cx + top_w, top_y),
        (cx + bot_w + s//10, bot_y),
        (cx - bot_w - s//10, bot_y),
    ], fill=C['beam_outer'])

    # Внутренний
    draw.polygon([
        (cx - top_w//2, top_y),
        (cx + top_w//2, top_y),
        (cx + bot_w, bot_y),
        (cx - bot_w, bot_y),
    ], fill=C['beam_inner'])

    # Кольца
    for i in range(4):
        t = (i + 1) / 5
        ry = top_y + (bot_y - top_y) * t * 0.75
        rw = int(top_w * (0.7 + t * 0.6))
        draw.ellipse([cx - rw//2, ry - s//100, cx + rw//2, ry + s//100],
                     fill=C['beam_ring'])


def _draw_files(draw, s, C, detail):
    """Файлы в луче."""
    if detail < 3:
        return
    cx = s // 2
    positions = [
        (-0.08, 0.48,  12, 1.0),
        ( 0.15, 0.44,  -8, 0.85),
        (-0.18, 0.58,  22, 0.7),
        ( 0.08, 0.63,  -3, 0.6),
        (-0.03, 0.52,   5, 0.5),
    ]
    for oxr, yr, angle, sc in positions:
        fx = cx + int(oxr * s)
        fy = int(s * yr)
        fw = max(3, int(s * 0.040 * sc))
        fh = max(4, int(s * 0.055 * sc))
        fi = Image.new('RGBA', (fw + 4, fh + 4), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fi)
        fd.rectangle([2, 2, fw, fh], fill=C['file_body'])
        for li in range(3):
            ly = fh * 0.25 + li * fh * 0.22
            fd.rectangle([fw * 0.15, ly, fw * 0.85, ly + max(1, fh//10)], fill=C['file_line'])
        fd.rectangle([2, 2, int(fw * 0.35), int(fh * 0.25)], fill=C['file_tab'])
        if angle:
            fi = fi.rotate(angle, expand=True, resample=Image.BICUBIC)
        if sc < 0.7:
            fi = fi.filter(ImageFilter.GaussianBlur(radius=0.6))
        draw.bitmap((int(fx - fi.width/2), int(fy - fi.height/2)), fi)


def _make_saucer(s, C, detail):
    """
    Создаёт летающую тарелку на прозрачном фоне.
    Возвращает (img, beam_origin_x, beam_origin_y) где beam_origin —
    центр нижней точки корпуса ПОСЛЕ поворота на 45° по часовой.
    """
    margin = int(s * 0.25)
    cs = s + margin * 2          # canvas size
    img = Image.new('RGBA', (cs, cs), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cs // 2
    cy = int(cs * 0.42)          # центр тарелки

    # ── Пропорции (ключевое изменение!) ─────
    body_w = int(cs * 0.62)
    body_h = int(cs * 0.18)      # плоский корпус
    dome_w = int(cs * 0.24)      # купол в 2.5 раза уже корпуса
    dome_h = int(cs * 0.22)      # по высоте чуть больше корпуса

    # Обводка
    lw = max(1, body_w // 60) if detail >= 2 else 0

    # ── Корпус (широкая плоская тарелка) ────
    bx0 = cx - body_w // 2
    by0 = cy - body_h // 2
    bx1 = cx + body_w // 2
    by1 = cy + body_h // 2
    bbox = [bx0, by0, bx1, by1]

    if detail >= 2:
        # Верхняя половина (светлая)
        top_bbox = bbox[:]; top_bbox[3] = cy
        draw.chord(top_bbox, 180, 360, fill=C['body'])
        # Нижняя половина (тень)
        bot_bbox = bbox[:]; bot_bbox[1] = cy
        draw.chord(bot_bbox, 0, 180, fill=C['body_shadow'])
        # Обводка
        draw.ellipse(bbox, outline=C['body_rim'], width=lw)
        # Блик по верхнему краю
        rim = bbox[:]; rim[3] = cy - body_h//3
        draw.chord(rim, 180, 360, fill=(min(255, C['body'][0]+40),
                                         min(255, C['body'][1]+40),
                                         min(255, C['body'][2]+40), 255))
    else:
        draw.ellipse(bbox, fill=C['body'])

    # ── Купол (маленький, наверху корпуса) ───
    dx0 = cx - dome_w // 2
    dy0 = cy - dome_h + body_h//2
    dx1 = cx + dome_w // 2
    dy1 = cy + body_h//2
    dome_bbox = [dx0, dy0, dx1, dy1]

    if detail >= 2:
        draw.ellipse(dome_bbox, fill=C['dome'], outline=C['dome_rim'], width=lw)
        # Блик
        gl = dome_bbox[:]; gl[3] = dy0 + dome_h * 0.5
        draw.ellipse(gl, fill=C['dome_glint'])
        # «Окно»
        win_y = dy1 - dome_h * 0.08
        draw.ellipse([cx - dome_w*0.30, win_y, cx + dome_w*0.30, win_y + dome_h*0.05],
                     fill=C['dome_glint'])
    else:
        draw.ellipse(dome_bbox, fill=C['dome'])

    # ── Поворот на 45° по часовой ─────
    rotated = img.rotate(-45, center=(cx, cy), expand=False, resample=Image.BICUBIC)

    # Точка выхода луча: низ корпуса (by1) после поворота
    dy = by1 - cy
    c45 = 0.70710678
    bx = cx + dy * c45
    by = cy + dy * c45

    return rotated, bx, by


def create_icon(size, theme='dark', detail=3):
    """Создать иконку. size=px, detail=3|2|1."""
    ss = 4 if detail >= 2 else 3
    s = size * ss
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    C = THEMES[theme]

    # Луч и файлы ПОД тарелкой
    _draw_beam(draw, s, C, detail)
    _draw_files(draw, s, C, detail)

    # Тарелка ПОВЕРХ
    saucer, bx, by = _make_saucer(s, C, detail)
    paste_x = s//2 - saucer.width//2
    paste_y = s//2 - saucer.height//2
    img.paste(saucer, (paste_x, paste_y), saucer)

    # Антиалиасинг
    return img.resize((size, size), Image.LANCZOS)


# ── ICO ──────────────────────────────────────
def make_ico(entries, path):
    """entries: [(size, PIL.Image), ...]"""
    import io
    pngs = []
    for _, im in entries:
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        pngs.append(buf.getvalue())

    n = len(pngs)
    hdr = struct.pack('<HHH', 0, 1, n)
    off = 6 + 16 * n
    dir = b''
    for i, im in enumerate([e[1] for e in entries]):
        w = im.width if im.width < 256 else 0
        h = im.height if im.height < 256 else 0
        dir += struct.pack('<BBBBHHII', w, h, 0, 0, 1, 32, len(pngs[i]), off)
        off += len(pngs[i])

    with open(path, 'wb') as f:
        f.write(hdr); f.write(dir)
        for d in pngs: f.write(d)
    print(f"  ✓ {path}  ({n} sizes, {off} bytes)")


# ── Генерация ────────────────────────────────
def generate_all(out='Assets'):
    os.makedirs(out, exist_ok=True)
    root = os.path.dirname(os.path.abspath(__file__))

    configs = [(256, 3, 'large'), (48, 2, 'medium'), (24, 1, 'tray')]

    for theme in ('dark', 'light'):
        print(f"\n🎨 {theme}")
        ico_all = []
        for size, detail, label in configs:
            img = create_icon(size, theme, detail)
            p = os.path.join(out, f'icon_{theme}_{label}.png')
            img.save(p)
            ico_all.append((size, img))
            print(f"  {label} ({size}×{size}) → {p}")

        # Дополнительные размеры для .ico
        extras = [
            (32, create_icon(32, theme, 2)),
            (16, create_icon(16, theme, 1)),
        ]
        for s, im in extras:
            ico_all.append((s, im))

        ico_path = os.path.join(root, f'icon_{theme}.ico')
        make_ico(ico_all, ico_path)

    # Основной .ico = dark
    src = os.path.join(root, 'icon_dark.ico')
    dst = os.path.join(root, 'icon.ico')
    if os.path.exists(src):
        shutil.copy(src, dst)
    print(f"\n✅ Готово. {out}/ , icon.ico")


if __name__ == '__main__':
    generate_all()
