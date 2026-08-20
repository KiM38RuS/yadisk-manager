#!/usr/bin/env python3
"""
YaDisk Manager — Icon Generator v3
═════════════════════════════════════════════
Летающая тарелка накренена на 45° вправо.
Луч бьёт от тарелки вниз.
Файлы засасываются в луч.

Всё рисуется на одном холсте, поворот — всей сцены.
"""

from PIL import Image, ImageDraw, ImageFilter
import os, struct, shutil, math
import numpy as np

# ── Палитры ──────────────────────────────────
THEMES = {
    'dark': {
        'body_top':      (175, 190, 205),
        'body_bottom':   (140, 155, 172),
        'body_outline':  (120, 135, 150),
        'body_highlight':(215, 225, 235),
        'dome':          (100, 190, 240),
        'dome_glint':    (180, 225, 255),
        'dome_rim':      (60, 140, 195),
        'beam':          (180, 240, 255, 40),
        'beam_core':     (220, 248, 255, 90),
        'beam_ring':     (200, 244, 255, 130),
        'file1':         (255, 220, 80),
        'file1_line':    (180, 150, 45),
        'file2':         (120, 200, 255),
        'file2_line':    (70, 140, 200),
        'file3':         (180, 255, 140),
        'file3_line':    (100, 180, 80),
    },
    'light': {
        'body_top':      (85, 100, 115),
        'body_bottom':   (60, 75, 90),
        'body_outline':  (45, 55, 70),
        'body_highlight':(115, 130, 148),
        'dome':          (50, 150, 210),
        'dome_glint':    (90, 185, 245),
        'dome_rim':      (30, 110, 170),
        'beam':          (60, 210, 255, 35),
        'beam_core':     (140, 225, 255, 80),
        'beam_ring':     (120, 220, 255, 110),
        'file1':         (220, 185, 50),
        'file1_line':    (155, 125, 30),
        'file2':         (80, 170, 230),
        'file2_line':    (50, 110, 170),
        'file3':         (130, 220, 100),
        'file3_line':    (70, 150, 50),
    }
}


def _draw_document(draw, x, y, w, h, color_body, color_line, angle=0):
    """Рисует документ (файл) на draw, возвращает offset для bitmap."""
    fi = Image.new('RGBA', (w + 6, h + 6), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fi)

    # Тело
    fd.rectangle([3, 3, w, h], fill=color_body)
    # Линии текста
    lh = max(1, h // 12)
    for i in range(4):
        ly = h * 0.2 + i * h * 0.2
        fd.rectangle([w * 0.15, ly, w * 0.85, ly + lh], fill=color_line)
    # Уголок
    fd.rectangle([3, 3, int(w * 0.35), int(h * 0.2)], fill=(min(255, color_body[0]+40),
                                                             min(255, color_body[1]+20),
                                                             min(255, color_body[2]-20)))

    if angle:
        fi = fi.rotate(angle, expand=True, resample=Image.BICUBIC)

    draw.bitmap((x, y), fi)


def _draw_folder(draw, x, y, w, h, color_body, color_line, angle=0):
    """Рисует папку."""
    fi = Image.new('RGBA', (w + 6, h + 6), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fi)

    # Задняя часть
    fd.rectangle([3, int(h*0.15)+3, w, h], fill=(max(0, color_body[0]-30),
                                                   max(0, color_body[1]-30),
                                                   max(0, color_body[2]-30)))
    # Передняя часть (с корешком)
    fd.rectangle([3, 3, int(w*0.4), int(h*0.2)], fill=color_line)
    fd.rectangle([3, int(h*0.2), w, h], fill=color_body)

    if angle:
        fi = fi.rotate(angle, expand=True, resample=Image.BICUBIC)

    draw.bitmap((x, y), fi)


def create_icon(size, theme='dark', detail=3):
    """Создать иконку. detail: 3=large, 2=medium, 1=tray."""
    ss = 4 if detail >= 2 else 3
    s = size * ss          # supersampled size
    C = THEMES[theme]

    # ── Холст (квадрат с запасом для поворота) ──
    margin = int(s * 0.35)
    cs = s + margin * 2    # canvas size
    img = Image.new('RGBA', (cs, cs), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Позиции (центр холста) ──
    cx = cs // 2
    cy = int(cs * 0.42)    # тарелка чуть выше центра, место для луча

    # ── Параметры тарелки ──
    body_w = int(cs * 0.55)
    body_h = int(cs * 0.22)      # корпус потолще
    dome_w = int(cs * 0.22)      # купол уже корпуса
    dome_h = int(cs * 0.20)

    bx0 = cx - body_w // 2
    by0 = cy - body_h // 2
    bx1 = cx + body_w // 2
    by1 = cy + body_h // 2
    bbox = [bx0, by0, bx1, by1]
    # by1 нужен для позиционирования файлов

    # ══════════════════════════════════════════
    # 1. ЛУЧ (под тарелкой)
    # ══════════════════════════════════════════
    if detail >= 2:
        beam_top_w = int(cs * 0.10)    # ширина у тарелки (узкий!)
        beam_bot_w = int(cs * 0.20)    # ширина внизу
        beam_top_y = by1               # от низа корпуса
        beam_bot_y = cs + 4            # до низа холста

        # Внешний луч (полупрозрачный)
        draw.polygon([
            (cx - beam_top_w, beam_top_y),
            (cx + beam_top_w, beam_top_y),
            (cx + beam_bot_w, beam_bot_y),
            (cx - beam_bot_w, beam_bot_y),
        ], fill=C['beam'])

        # Внутренний луч (ярче, уже)
        draw.polygon([
            (cx - beam_top_w//2, beam_top_y + int(cs*0.02)),
            (cx + beam_top_w//2, beam_top_y + int(cs*0.02)),
            (cx + int(beam_bot_w * 0.7), beam_bot_y),
            (cx - int(beam_bot_w * 0.7), beam_bot_y),
        ], fill=C['beam_core'])

        # Кольца/волны в луче
        for i in range(5):
            t = (i + 1) / 6
            ry = beam_top_y + (beam_bot_y - beam_top_y) * t * 0.8
            rw = int(beam_top_w * (1.0 + t * 3.0))
            draw.ellipse([cx - rw, ry - int(s*0.005), cx + rw, ry + int(s*0.005)],
                         fill=C['beam_ring'])

    # ══════════════════════════════════════════
    # 2. ФАЙЛЫ В ЛУЧЕ (крупнее, ближе к тарелке)
    # ══════════════════════════════════════════
    if detail >= 3:
        # Позиции файлов относительно выхода луча (by1, cx)
        # Сдвиг от центра: (dx_ratio, dy_ratio, type, angle_deg, scale)
        # dy считается от by1 вниз
        files = [
            (-0.18,  0.08, 'doc',    15,  1.0),
            ( 0.20,  0.04, 'folder', -10,  0.9),
            (-0.12,  0.18, 'doc',    25,  0.75),
            ( 0.22,  0.14, 'doc',    -8,  0.7),
            (-0.24,  0.32, 'folder',  30,  0.6),
            ( 0.08,  0.06, 'doc',     5,  1.0),
            ( 0.16,  0.26, 'doc',   -20,  0.65),
        ]

        for oxr, dyr, ftype, angle, sc in files:
            fx = cx + int(oxr * cs)
            fy = int(by1 + dyr * cs)
            # Крупнее — 10% от canvas для полного, 6% для мелкого
            fw = max(12, int(cs * 0.070 * sc))
            fh = max(16, int(cs * 0.095 * sc))

            if ftype == 'folder':
                _draw_folder(draw, int(fx - fw/2), int(fy - fh/2), fw, fh,
                             C['file2'], C['file2_line'], angle)
            else:
                # Чередуем цвета документов
                color_idx = hash(str(oxr) + str(dyr)) % 3
                if color_idx == 0:
                    body, line = C['file1'], C['file1_line']
                elif color_idx == 1:
                    body, line = C['file2'], C['file2_line']
                else:
                    body, line = C['file3'], C['file3_line']
                _draw_document(draw, int(fx - fw/2), int(fy - fh/2), fw, fh,
                               body, line, angle)

    # ══════════════════════════════════════════
    # 3. ТАРЕЛКА
    # ══════════════════════════════════════════
    if detail >= 2:
        # Корпус: верхняя половина
        top_bbox = bbox[:]; top_bbox[3] = cy
        draw.chord(top_bbox, 180, 360, fill=C['body_top'])
        # Корпус: нижняя половина
        bot_bbox = bbox[:]; bot_bbox[1] = cy
        draw.chord(bot_bbox, 0, 180, fill=C['body_bottom'])
        # Обводка
        lw = max(1, body_w // 50)
        draw.ellipse(bbox, outline=C['body_outline'], width=lw)
        # Блик сверху
        hl = bbox[:]; hl[3] = cy - body_h//3
        draw.chord(hl, 180, 360, fill=C['body_highlight'])
    else:
        # Упрощённый силуэт для трея
        draw.ellipse(bbox, fill=C['body_top'])

    # Купол
    dx0 = cx - dome_w // 2
    dy0 = cy - dome_h + body_h//2
    dx1 = cx + dome_w // 2
    dy1 = cy + body_h//2
    dome_bbox = [dx0, dy0, dx1, dy1]

    if detail >= 2:
        draw.ellipse(dome_bbox, fill=C['dome'], outline=C['dome_rim'], width=lw)
        # Блик на куполе
        dgl = dome_bbox[:]; dgl[3] = dy0 + dome_h * 0.45
        draw.ellipse(dgl, fill=C['dome_glint'])
    else:
        draw.ellipse(dome_bbox, fill=C['dome'])

    # ══════════════════════════════════════════
    # 4. ПОВОРОТ ВСЕЙ СЦЕНЫ НА 45° CW
    # ══════════════════════════════════════════
    rotated = img.rotate(-45, center=(cx, cy), expand=False, resample=Image.BICUBIC)

    # ══════════════════════════════════════════
    # 5. УМНАЯ ВЫРЕЗКА — ПО ЦЕНТРУ НЕПРОЗРАЧНОГО КОНТЕНТА
    # ══════════════════════════════════════════
    alpha = rotated.split()[3]
    alpha_arr = np.array(alpha)
    # Pixels with alpha > 30 are considered "visible content"
    mask = alpha_arr > 200
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any() and cols.any():
        y0 = int(np.where(rows)[0][0])
        y1 = int(np.where(rows)[0][-1]) + 1
        x0 = int(np.where(cols)[0][0])
        x1 = int(np.where(cols)[0][-1]) + 1
        ccx = (x0 + x1) // 2
        ccy = (y0 + y1) // 2
        left = int(max(0, min(ccx - s // 2, cs - s)))
        top = int(max(0, min(ccy - s // 2, cs - s)))
    else:
        left = (cs - s) // 2
        top = (cs - s) // 2

    cropped = rotated.crop((left, top, left + s, top + s))

    # Антиалиасинг
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
    print(f"  ✓ {path}  ({n} sizes)")


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

        # Доп. размеры для .ico
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
