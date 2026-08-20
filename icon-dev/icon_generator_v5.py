#!/usr/bin/env python3
"""
YaDisk Manager — Icon Generator v5
═════════════════════════════════════════════
Подход: рисуем всё сразу в перспективе, без поворота.
Диск — широкий эллипс, купол — маленький эллипс.
Луч идёт строго вниз от центра диска.
Файлы — в луче, с чёткими контурами.
Тарелка — в центре иконки, акцент на ней.
═════════════════════════════════════════════
"""
from PIL import Image, ImageDraw, ImageFilter
import os, struct, shutil

VERSION = "0.9.4"

# ─── Themes ───
THEMES = {
    'dark': {
        'body':         (180, 195, 210),
        'body_shadow':  (100, 115, 130),
        'body_hl':      (210, 220, 230),
        'dome':         (60, 140, 200),
        'dome_hl':      (100, 180, 240),
        'beam_outer':   (180, 230, 255, 35),
        'beam_inner':   (180, 230, 255, 80),
        'beam_core':    (220, 245, 255, 160),
        'file_yellow':  (240, 200, 60),
        'file_y_line':  (180, 140, 30),
        'file_blue':    (80, 150, 220),
        'file_b_line':  (50, 100, 160),
        'file_green':   (80, 200, 100),
        'file_g_line':  (50, 140, 70),
    },
    'light': {
        'body':         (130, 145, 160),
        'body_shadow':  (70, 85, 100),
        'body_hl':      (160, 175, 190),
        'dome':         (50, 120, 180),
        'dome_hl':      (80, 160, 220),
        'beam_outer':   (160, 210, 240, 30),
        'beam_inner':   (160, 210, 240, 70),
        'beam_core':    (200, 230, 250, 140),
        'file_yellow':  (220, 180, 50),
        'file_y_line':  (160, 120, 20),
        'file_blue':    (70, 130, 200),
        'file_b_line':  (40, 80, 140),
        'file_green':   (70, 180, 90),
        'file_g_line':  (40, 120, 60),
    },
}

def _draw_doc(draw, x, y, w, h, fill, line_color, angle):
    """Рисует документ (прямоугольник с загнутым уголком)."""
    from PIL import Image as PILImage
    pad = 6
    tmp = PILImage.new('RGBA', (w + pad, h + pad), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    cx2, cy2 = tmp.width // 2, tmp.height // 2

    tdraw.rectangle([cx2 - w//2, cy2 - h//2, cx2 + w//2 - 3, cy2 + h//2], fill=fill, outline=line_color, width=1)
    # corner fold
    tdraw.polygon([(cx2 + w//2 - 3, cy2 - h//2), (cx2 + w//2 - 3, cy2 - h//2 + 6), (cx2 + w//2 + 3, cy2 - h//2 + 3)], fill=fill, outline=line_color)

    rotated = tmp.rotate(angle, expand=False, center=(cx2, cy2), resample=PILImage.BICUBIC)
    # Draw so center of document = (x, y)
    draw.bitmap((int(x - cx2), int(y - cy2)), rotated)

def _draw_folder(draw, x, y, w, h, fill, line_color, angle):
    """Рисует папку."""
    from PIL import Image as PILImage
    pad = 6
    tmp = PILImage.new('RGBA', (w + pad, h + pad), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    cx2, cy2 = tmp.width // 2, tmp.height // 2
    hw, hh = w // 2, h // 2

    tab = hh // 3
    tdraw.polygon([(cx2 - hw, cy2 - hh + tab), (cx2 - hw + tab, cy2 - hh), (cx2 + hw, cy2 - hh), (cx2 + hw, cy2 + hh), (cx2 - hw, cy2 + hh)], fill=fill, outline=line_color, width=1)

    rotated = tmp.rotate(angle, expand=False, center=(cx2, cy2), resample=PILImage.BICUBIC)
    draw.bitmap((int(x - cx2), int(y - cy2)), rotated)

def create_icon(size, theme='dark', detail=3):
    """Создать иконку без поворота — всё рисуется сразу."""
    ss = 4 if detail >= 2 else 3
    s = size * ss
    C = THEMES[theme]
    margin = int(s * 0.05)
    cs = s + margin * 2
    cx = cs // 2
    cy = cs // 2

    img = Image.new('RGBA', (cs, cs), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 1. ДИСК (самый большой элемент) ──
    disc_w = int(cs * 0.68)
    disc_h = int(cs * 0.32)
    bx0 = cx - disc_w // 2
    by0 = cy - disc_h // 2 - int(cs * 0.06)
    bx1 = cx + disc_w // 2
    by1 = by0 + disc_h
    bbox = [bx0, by0, bx1, by1]

    # Базовый диск
    draw.ellipse(bbox, fill=C['body'])
    if detail >= 2:
        # Тень — нижняя половина
        b = bbox[:]; b[1] = cy - int(cs * 0.06)
        draw.chord(b, 0, 180, fill=C['body_shadow'])
        # Блик — верхняя четверть
        hl = bbox[:]; hl[3] = by0 + disc_h // 3
        draw.chord(hl, 180, 360, fill=C['body_hl'])

    # ── 2. КУПОЛ (маленький, в верхней части диска) ──
    dome_w = int(cs * 0.14)
    dome_h = int(cs * 0.10)
    dx0 = cx - dome_w // 2
    dy0 = by0 - dome_h + disc_h // 3
    dx1 = cx + dome_w // 2
    dy1 = dy0 + dome_h
    dome_bbox = [dx0, dy0, dx1, dy1]

    draw.ellipse(dome_bbox, fill=C['dome'])
    if detail >= 2:
        gl = dome_bbox[:]; gl[3] = dy0 + dome_h * 0.4
        draw.ellipse(gl, fill=C['dome_hl'])

    # ── 3. ЛУЧ (строго вниз, узкий) ──
    if detail >= 2:
        beam_y0 = by1
        beam_y1 = cs
        bw0 = int(cs * 0.03)
        bw1 = int(cs * 0.08)

        draw.polygon([
            (cx - bw0, beam_y0), (cx + bw0, beam_y0),
            (cx + bw1, beam_y1), (cx - bw1, beam_y1),
        ], fill=C['beam_outer'])

        draw.polygon([
            (cx - max(1, bw0//2), beam_y0 + int(cs*0.02)),
            (cx + max(1, bw0//2), beam_y0 + int(cs*0.02)),
            (cx + int(bw1*0.5), beam_y1),
            (cx - int(bw1*0.5), beam_y1),
        ], fill=C['beam_inner'])

        # Кольца
        for i in range(3):
            t = (i + 1) / 4
            ry = int(beam_y0 + (beam_y1 - beam_y0) * t * 0.7)
            rw = int(bw0 * (1.0 + t * 5.0))
            draw.ellipse([cx - rw, ry - 1, cx + rw, ry + 1], fill=C['beam_core'])

    # ── 4. ФАЙЛЫ (крупные, с чёткими линиями) ──
    if detail >= 3:
        file_data = [
            (-0.08, 0.06, 'doc_y',    15,  1.4),
            ( 0.08, 0.03, 'folder_b', -12,  1.3),
            (-0.16, 0.20, 'doc_g',    30,  1.1),
            ( 0.16, 0.15, 'doc_y',   -18,  1.0),
            ( 0.00, 0.10, 'doc_b',    10,  1.4),
            ( 0.10, 0.35, 'doc_g',   -25,  0.9),
        ]
        color_map = {
            'doc_y':    (C['file_yellow'], C['file_y_line']),
            'doc_b':    (C['file_blue'], C['file_b_line']),
            'doc_g':    (C['file_green'], C['file_g_line']),
            'folder_b': (C['file_blue'], C['file_b_line']),
        }
        for oxr, dyr, ftype, angle, sc in file_data:
            fx = cx + int(oxr * cs)
            fy = int(by1 + dyr * cs)
            fw = max(20, int(cs * 0.07 * sc))
            fh = max(26, int(cs * 0.09 * sc))
            _draw_doc(draw, fx, fy, fw, fh,
                      *color_map[ftype], angle)

    # ── 5. ОБРЕЗКА ──
    # Обрезаем по bbox непрозрачных элементов
    from PIL import Image as PILImage
    import numpy as np
    alpha = np.array(img.split()[3])
    mask = alpha > 10
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any() and cols.any():
        y0 = int(np.where(rows)[0][0])
        y1 = int(np.where(rows)[0][-1]) + 1
        x0 = int(np.where(cols)[0][0])
        x1 = int(np.where(cols)[0][-1]) + 1
        # Берем квадрат по центру
        ch = y1 - y0
        cw = x1 - x0
        side = max(ch, cw)
        side = min(side, cs)
        ccx = (x0 + x1) // 2
        ccy = (y0 + y1) // 2
        ccy = int(ccy * 1.02)  # чуть смещаем вниз, чтобы тарелка не была вверху

        left = max(0, min(ccx - side // 2, cs - side))
        top = max(0, min(ccy - side // 2, cs - side))
        cropped = img.crop((left, top, left + side, top + side))
    else:
        cropped = img

    return cropped.resize((size, size), Image.LANCZOS)

def save_ico(pngs, path):
    """Сохраняет .ico с несколькими размерами."""
    sizes = [(256,256), (64,64), (48,48), (32,32), (16,16)]
    with open(path, 'wb') as f:
        f.write(struct.pack('<HHH', 0, 1, len(sizes)))
        offsets = []
        data_list = []
        for w, h in sizes:
            # Resize closest PNG
            src = max(pngs, key=lambda p: p.size[0] if p.size[0] <= w else 0)
            if src.size[0] != w:
                img = src.resize((w, h), Image.LANCZOS)
            else:
                img = src
            # BMP data
            bmp = img.tobytes("raw", "BGRA")
            data_list.append((w, h, bmp))
        offset = 6 + 16 * len(sizes)
        for i, (w, h, bmp) in enumerate(data_list):
            size = 40 + len(bmp)
            offsets.append((size, offset))
            offset += size
        for (w, h, bmp), (size, off) in zip(data_list, offsets):
            bw = 0 if w >= 256 else w
            bh = 0 if h >= 256 else h
            f.write(struct.pack('<BBBBHHII', bw, bh, 0, 0, 1, 32, size, off))
        for w, h, bmp in data_list:
            # BMP header
            f.write(struct.pack('<IiiHHIIiiII', 40, w, h * 2, 1, 32, 0, len(bmp), 0, 0, 0, 0))
            f.write(bmp)

def generate_all():
    os.makedirs('output', exist_ok=True)

    configs = [
        ('dark', 3, 256, '_large'),
        ('dark', 2,  48, '_medium'),
        ('dark', 1,  24, '_tray'),
        ('light', 3, 256, '_large'),
        ('light', 2,  48, '_medium'),
        ('light', 1,  24, '_tray'),
    ]
    pngs_dark = []
    pngs_light = []

    for theme, detail, size, suffix in configs:
        label = f"{theme}{suffix}"
        print(f"  {label} ({size}×{size}) → output/icon_{label}.png")
        img = create_icon(size, theme, detail)
        path = f"output/icon_{label}.png"
        img.save(path)
        if theme == 'dark':
            pngs_dark.append(img)
        else:
            pngs_light.append(img)

    save_ico(pngs_dark, "icon_dark.ico")
    print(f"  ✓ icon_dark.ico")
    save_ico(pngs_light, "icon_light.ico")
    print(f"  ✓ icon_light.ico")
    # Combined
    all_pngs = pngs_dark + pngs_light
    save_ico(all_pngs, "icon.ico")
    print(f"  ✓ icon.ico")

if __name__ == '__main__':
    import sys
    print("🎨 v5 — No rotation, perspective disc")
    generate_all()
    print("✅ Готово.")
