#!/usr/bin/env python3
"""
YaDisk Manager — Icon Generator v4
═════════════════════════════════════════════
Classic UFO shape: wide flat disc + small dome.
Narrow beam from bottom. Files being pulled in.
All elements on one canvas, rotated 45° together.
Saucer is the main focus (large, centered).
"""

from PIL import Image, ImageDraw, ImageFilter
import os, struct, shutil, math
import numpy as np

# ── Палитры ──────────────────────────────────
THEMES = {
    'dark': {
        'body':          (175, 190, 205),
        'body_shadow':   (130, 150, 168),
        'body_rim':      (110, 125, 140),
        'body_hl':       (210, 220, 230),
        'dome':          (100, 190, 240),
        'dome_hl':       (180, 225, 255),
        'dome_rim':      (60, 140, 195),
        'lights':        (255, 220, 60),
        'beam_outer':    (160, 230, 255, 22),
        'beam_inner':    (200, 245, 255, 70),
        'beam_core':     (225, 250, 255, 130),
        'file_yellow':   (255, 220, 60),
        'file_y_line':   (180, 150, 40),
        'file_blue':     (130, 210, 255),
        'file_b_line':   (80, 150, 200),
        'file_green':    (180, 255, 130),
        'file_g_line':   (100, 180, 70),
    },
    'light': {
        'body':          (85, 100, 115),
        'body_shadow':   (60, 75, 90),
        'body_rim':      (45, 55, 70),
        'body_hl':       (115, 130, 148),
        'dome':          (50, 150, 210),
        'dome_hl':       (90, 185, 245),
        'dome_rim':      (30, 110, 170),
        'lights':        (220, 185, 40),
        'beam_outer':    (60, 210, 255, 18),
        'beam_inner':    (100, 220, 255, 55),
        'beam_core':     (140, 230, 255, 100),
        'file_yellow':   (220, 185, 40),
        'file_y_line':   (155, 125, 25),
        'file_blue':     (90, 180, 230),
        'file_b_line':   (55, 120, 175),
        'file_green':    (140, 220, 90),
        'file_g_line':   (80, 155, 55),
    }
}


def _draw_doc(draw, x, y, w, h, body, lines, angle=0):
    """Рисует документ."""
    fi = Image.new('RGBA', (w + 6, h + 6), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fi)
    fd.rectangle([3, 3, w, h], fill=body)
    lh = max(1, h // 14)
    for i in range(4):
        ly = h * 0.2 + i * h * 0.19
        fd.rectangle([int(w * 0.15), int(ly), int(w * 0.85), int(ly + lh)], fill=lines)
    fd.rectangle([3, 3, int(w * 0.35), int(h * 0.2)], fill=(min(255, body[0]+40),
                                                              min(255, body[1]+20),
                                                              max(0, body[2]-20)))
    if angle:
        fi = fi.rotate(angle, expand=True, resample=Image.BICUBIC)
    draw.bitmap((x, y), fi)


def _draw_folder(draw, x, y, w, h, body, lines, angle=0):
    """Рисует папку."""
    fi = Image.new('RGBA', (w + 6, h + 6), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fi)
    fd.rectangle([3, int(h*0.15)+3, w, h], fill=(max(0, body[0]-30),
                                                   max(0, body[1]-30),
                                                   max(0, body[2]-30)))
    fd.rectangle([3, 3, int(w*0.4), int(h*0.2)], fill=lines)
    fd.rectangle([3, int(h*0.2), w, h], fill=body)
    if angle:
        fi = fi.rotate(angle, expand=True, resample=Image.BICUBIC)
    draw.bitmap((x, y), fi)


def create_icon(size, theme='dark', detail=3):
    """
    Создать иконку.
    detail=3: large (256px) — полная детализация
    detail=2: medium (48px) — тарелка + луч, без файлов
    detail=1: tray (24px) — только тарелка
    """
    ss = 4 if detail >= 2 else 3
    s = size * ss
    C = THEMES[theme]

    margin = int(s * 0.45)  # more margin for the rotation
    cs = s + margin * 2
    cx = cs // 2
    cy = int(cs * 0.46)

    # ── Параметры ──
    tilt_deg = 30                     # наклон — 30° вместо 45°
    disc_w = int(cs * 0.64)           # ширина диска — большой
    disc_h = int(cs * 0.18)           # толщина
    dome_w = int(cs * 0.12)           # купол — маленький
    dome_h = int(cs * 0.10)

    bx0 = cx - disc_w // 2
    by0 = cy - disc_h // 2
    bx1 = cx + disc_w // 2
    by1 = cy + disc_h // 2
    bbox = [bx0, by0, bx1, by1]

    lw = max(1, disc_w // 90) if detail >= 2 else 0

    img = Image.new('RGBA', (cs, cs), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 1. ДИСК ──
    # Единый эллипс — базовый цвет
    draw.ellipse(bbox, fill=C['body'])
    if detail >= 2:
        # Тёмная нижняя половина (объём)
        b = bbox[:]; b[1] = cy
        draw.chord(b, 0, 180, fill=C['body_shadow'])
        # Светлая полоса сверху (блик)
        hl = bbox[:]; hl[3] = cy - disc_h // 4
        draw.chord(hl, 180, 360, fill=C['body_hl'])

    # ── 2. КУПОЛ ──
    dx0 = int(cx - dome_w // 2)
    dy0 = int(cy - dome_h + disc_h // 2)
    dx1 = int(cx + dome_w // 2)
    dy1 = int(cy + disc_h // 2)
    dome_bbox = [dx0, dy0, dx1, dy1]

    if detail >= 2:
        draw.ellipse(dome_bbox, fill=C['dome'])
        gl = dome_bbox[:]; gl[3] = dy0 + dome_h * 0.3
        draw.ellipse(gl, fill=C['dome_hl'])
    else:
        draw.ellipse(dome_bbox, fill=C['dome'])

    # ── 3. ЛУЧ ──
    if detail >= 2:
        bw_top = max(2, int(cs * 0.03))
        bw_bot = max(4, int(cs * 0.08))
        by_start = by1
        by_end = cs + 4

        draw.polygon([
            (cx - bw_top, by_start),
            (cx + bw_top, by_start),
            (cx + bw_bot, by_end),
            (cx - bw_bot, by_end),
        ], fill=C['beam_outer'])

        draw.polygon([
            (cx - max(1, bw_top//3), by_start + int(cs*0.01)),
            (cx + max(1, bw_top//3), by_start + int(cs*0.01)),
            (cx + int(bw_bot*0.4), by_end),
            (cx - int(bw_bot*0.4), by_end),
        ], fill=C['beam_inner'])

        # Кольца
        for i in range(3):
            t_ratio = (i + 1) / 4
            ry = int(by_start + (by_end - by_start) * t_ratio * 0.6)
            rw = int(bw_top * (1.0 + t_ratio * 5.0))
            draw.ellipse([cx - rw, ry - 1, cx + rw, ry + 1], fill=C['beam_core'])

    # ── 4. ФАЙЛЫ ──
    if detail >= 3:
        file_data = [
            (-0.10,  0.06, 'doc_y',    10,  1.0),
            ( 0.12,  0.04, 'folder_b',  -8,  0.9),
            (-0.16,  0.18, 'doc_g',    22,  0.7),
            ( 0.16,  0.15, 'doc_y',   -12,  0.65),
            (-0.05,  0.08, 'doc_b',     5,  1.0),
            ( 0.08,  0.30, 'doc_g',   -18,  0.55),
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
            fw = max(14, int(cs * 0.048 * sc))
            fh = max(18, int(cs * 0.065 * sc))
            if ftype.startswith('folder'):
                _draw_folder(draw, int(fx - fw/2), int(fy - fh/2), fw, fh,
                             *color_map[ftype], angle)
            else:
                _draw_doc(draw, int(fx - fw/2), int(fy - fh/2), fw, fh,
                          *color_map[ftype], angle)

    # ── 5. ПОВОРОТ + ВЫРЕЗКА ──
    rotated = img.rotate(-tilt_deg, center=(cx, cy), expand=False, resample=Image.BICUBIC)

    alpha_arr = np.array(rotated.split()[3])
    mask = alpha_arr > 180
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any() and cols.any():
        y0 = int(np.where(rows)[0][0])
        y1 = int(np.where(rows)[0][-1]) + 1
        x0 = int(np.where(cols)[0][0])
        x1 = int(np.where(cols)[0][-1]) + 1
        ccx = (x0 + x1) // 2
        ccy = int((y0 + y1) // 2 * 0.97)  # чуть ниже, чтобы луч не вылезал
        left = int(max(0, min(ccx - s // 2, cs - s)))
        top = int(max(0, min(ccy - s // 2, cs - s)))
    else:
        left = (cs - s) // 2
        top = (cs - s) // 2

    cropped = rotated.crop((left, top, left + s, top + s))
    return cropped.resize((size, size), Image.LANCZOS)


# ── ICO ──────────────────────────────────────
def make_ico(entries, path):
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
    print(f"  ✓ {path} ({n} sizes)")


# ── Генерация ────────────────────────────────
def generate_all(out='output'):
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
        for s in (32, 16):
            d = 2 if s >= 32 else 1
            ico_all.append((s, create_icon(s, theme, d)))
        ico_path = os.path.join(root, f'icon_{theme}.ico')
        make_ico(ico_all, ico_path)

    src = os.path.join(root, 'icon_dark.ico')
    dst = os.path.join(root, 'icon.ico')
    if os.path.exists(src):
        shutil.copy(src, dst)
    print(f"\n✅ Готово. {out}/ , icon.ico")


if __name__ == '__main__':
    generate_all()
