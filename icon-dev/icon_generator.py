#!/usr/bin/env python3
"""
YaDisk Manager Icon Generator
═══════════════════════════════════════════════
Генерирует иконки для YaDisk Manager: летающая тарелка
с лучом, засасывающим файлы.

Режимы:
  - large  (256×256)  — полная детализация, луч, файлы
  - medium (48×48)    — упрощённо, луч, 1-2 файла
  - tray   (24×24)    — только силуэт тарелки

Темы:
  - dark  — светлая тарелка (для тёмной темы Windows)
  - light — тёмная тарелка (для светлой темы Windows)

Формат вывода:
  - .ico (многослойный: 256+48+32+24+16)
  - .png (отдельные размеры)
═══════════════════════════════════════════════
"""

from PIL import Image, ImageDraw, ImageFilter
import math, os, struct

# ──────────────────────────────────────────────
# Цветовые палитры
# ──────────────────────────────────────────────
THEMES = {
    'dark': {
        'body_top':       (175, 190, 205),
        'body_bottom':    (140, 155, 172),
        'body_highlight': (210, 220, 230),
        'body_line':      (120, 135, 150),
        'dome':           (100, 190, 240),
        'dome_highlight': (170, 220, 255),
        'dome_line':      (60, 140, 195),
        'beam_inner':     (180, 240, 255, 55),
        'beam_outer':     (100, 200, 255, 18),
        'beam_ray':       (220, 248, 255, 120),
        'file_body':      (255, 220, 80),
        'file_line':      (180, 150, 45),
        'file_tab':       (220, 180, 50),
        'outline':        (60, 70, 85, 160),
    },
    'light': {
        'body_top':       (85, 100, 115),
        'body_bottom':    (60, 75, 90),
        'body_highlight': (115, 130, 148),
        'body_line':      (45, 55, 70),
        'dome':           (50, 150, 210),
        'dome_highlight': (90, 185, 245),
        'dome_line':      (30, 110, 170),
        'beam_inner':     (60, 210, 255, 50),
        'beam_outer':     (40, 170, 240, 15),
        'beam_ray':       (140, 225, 255, 100),
        'file_body':      (220, 185, 50),
        'file_line':      (155, 125, 30),
        'file_tab':       (185, 150, 35),
        'outline':        (190, 200, 210, 160),
    }
}


def _draw_saucer(draw, cx, cy, body_w, body_h, dome_w, dome_h, C, detail):
    """
    Рисует летающую тарелку (без наклона) на canvas draw.
    Возвращает bounding box корпуса для расчёта луча.
    """
    # --- Корпус (нижняя половина эллипса, с градиентоподобной заливкой) ---
    bbox = [cx - body_w//2, cy - body_h//2, cx + body_w//2, cy + body_h//2]
    line_w = max(1, body_w // 48) if detail >= 2 else 0

    if detail >= 2:
        # Нижняя часть (тень)
        draw.chord(bbox, 0, 180, fill=C['body_bottom'])
        # Верхняя часть (светлая)
        top_bbox = bbox[:]
        top_bbox[3] = cy
        draw.chord(top_bbox, 180, 360, fill=C['body_top'])
        # Обводка
        draw.ellipse(bbox, outline=C['body_line'], width=line_w)
        # Блики по краям
        highlight_bbox = bbox[:]
        highlight_bbox[1] = cy - body_h//2
        highlight_bbox[3] = cy - body_h//4
        draw.chord(highlight_bbox, 180, 360, fill=C['body_highlight'])
    else:
        # Минимальный силуэт
        draw.ellipse(bbox, fill=C['body_top'])

    # --- Купол ---
    dome_bbox = [cx - dome_w//2, cy - dome_h + body_h//4,
                 cx + dome_w//2, cy + body_h//2]
    if detail >= 2:
        draw.ellipse(dome_bbox, fill=C['dome'], outline=C['dome_line'], width=line_w)
        # Блик на куполе
        dh_bbox = dome_bbox[:]
        dh_bbox[1] = dome_bbox[1]
        dh_bbox[3] = dome_bbox[1] + dome_h * 0.55
        draw.ellipse(dh_bbox, fill=C['dome_highlight'])
        # Оконная линия
        win_y = dome_bbox[3] - dome_h * 0.08
        draw.ellipse([cx - dome_w*0.35, win_y, cx + dome_w*0.35, win_y + dome_h*0.05],
                     fill=C['dome_highlight'])
    else:
        draw.ellipse(dome_bbox, fill=C['dome'])

    return bbox


def _draw_beam(draw, s, C, detail):
    """
    Рисует луч (центрированный, во всю высоту нижней половины).
    Возвращает bounding box области луча.
    """
    if detail < 2:
        return None  # для tray луча нет

    cx = s // 2
    top_y = int(s * 0.42)
    bot_y = s + 2  # чуть за край для перекрытия

    top_w = int(s * 0.28)
    bot_w = int(s * 0.55)

    # Внешний луч (широкий, полупрозрачный)
    outer = [
        (cx - top_w, top_y),
        (cx + top_w, top_y),
        (cx + bot_w + int(s*0.1), bot_y),
        (cx - bot_w - int(s*0.1), bot_y),
    ]
    draw.polygon(outer, fill=C['beam_outer'])

    # Внутренний луч (яркий, плотный)
    inner = [
        (cx - top_w//2, top_y),
        (cx + top_w//2, top_y),
        (cx + bot_w, bot_y),
        (cx - bot_w, bot_y),
    ]
    draw.polygon(inner, fill=C['beam_inner'])

    # Энергетические кольца / полосы в луче
    for i in range(4):
        t = (i + 1) / 5
        ry = top_y + (bot_y - top_y) * t * 0.7
        rw = int(top_w * (0.8 + t * 0.4))
        draw.ellipse([cx - rw//2, ry - s//80, cx + rw//2, ry + s//80],
                     fill=C['beam_ray'])

    return [cx - bot_w, top_y, cx + bot_w, bot_y]


def _draw_files(draw, s, C, detail):
    """Рисует файлы, втягиваемые в луч."""
    if detail < 3:
        return  # только для large

    cx = s // 2
    # Позиции файлов: (offset_x_ratio, y_ratio, angle_deg, scale_ratio)
    positions = [
        (-0.10, 0.52,  15, 1.0),
        ( 0.18, 0.48, -10, 0.85),
        (-0.20, 0.62,  25, 0.7),
        ( 0.10, 0.67,  -5, 0.6),
        (-0.05, 0.56,   5, 0.5),
    ]

    for oxr, yr, angle, scale in positions:
        fx = cx + int(oxr * s)
        fy = int(s * yr)

        fw = int(s * 0.045 * scale)
        fh = int(s * 0.06 * scale)
        if fw < 3 or fh < 4:
            continue

        file_img = Image.new('RGBA', (fw + 4, fh + 4), (0, 0, 0, 0))
        fd = ImageDraw.Draw(file_img)

        # Тело файла
        fd.rectangle([2, 2, fw, fh], fill=C['file_body'])
        # Линии «текста»
        line_h = max(1, fh // 10)
        for li in range(3):
            ly = fh * 0.25 + li * fh * 0.22
            fd.rectangle([fw * 0.15, ly, fw * 0.85, ly + line_h], fill=C['file_line'])
        # Уголок (tab)
        fd.rectangle([2, 2, int(fw * 0.35), int(fh * 0.25)], fill=C['file_tab'])

        if angle != 0:
            file_img = file_img.rotate(angle, expand=True, resample=Image.BICUBIC)

        # Немного размытия, чтобы показать движение
        if scale < 0.7:
            file_img = file_img.filter(ImageFilter.GaussianBlur(radius=0.5))

        px = int(fx - file_img.width / 2)
        py = int(fy - file_img.height / 2)
        draw.bitmap((px, py), file_img)


def _draw_saucer_rotated(s, C, detail):
    """
    Рисует тарелку на отдельном холсте, поворачивает на 45° вправо (CW).
    Возвращает (img, beam_origin_x, beam_origin_y).
    beam_origin — центр низа корпуса после поворота.
    """
    margin = int(s * 0.2)
    canvas_size = s + margin * 2
    img = Image.new('RGBA', (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = canvas_size // 2
    # Тарелка немного выше центра, чтобы хватило места для луча
    cy = int(canvas_size * 0.38)
    body_w = int(canvas_size * 0.60)
    body_h = int(canvas_size * 0.30)
    dome_w = int(canvas_size * 0.40)
    dome_h = int(canvas_size * 0.28)

    bbox = _draw_saucer(draw, cx, cy, body_w, body_h, dome_w, dome_h, C, detail)

    # Центр низа корпуса (до поворота)
    bottom_y = bbox[3]  # y1 нижняя граница эллипса
    dy = bottom_y - cy   # положительное — ниже центра

    # Поворот на 45° по часовой стрелке (CW = -45°)
    rotated = img.rotate(-45, center=(cx, cy), expand=False, resample=Image.BICUBIC)

    # После поворота: bottom точка смещается
    cos45 = 0.70710678
    beam_origin_x = cx + dy * cos45
    beam_origin_y = cy + dy * cos45

    return rotated, beam_origin_x, beam_origin_y


# ──────────────────────────────────────────────
# Основная функция генерации иконки
# ──────────────────────────────────────────────

def create_icon(size, theme='dark', detail=3):
    """
    Создать иконку заданного размера.

    Параметры:
      size   — целевой размер в px (256, 48, 24...)
      theme  — 'dark' | 'light'
      detail — 3 (large, full), 2 (medium, beam+simplified), 1 (tray, outline only)
    """
    ss = 4 if detail >= 2 else 3  # supersampling factor
    s = size * ss

    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    C = THEMES[theme]

    # 1. Луч (на основном холсте, до тарелки — чтобы быть под ней)
    _draw_beam(draw, s, C, detail)

    # 2. Файлы в луче (тоже под тарелкой)
    _draw_files(draw, s, C, detail)

    # 3. Тарелка (повёрнутая) — поверх
    saucer_img, bx, by = _draw_saucer_rotated(s, C, detail)
    # Позиционируем: центр тарелки (bx, by) совмещаем с центром основного холста
    paste_x = s // 2 - saucer_img.width // 2
    paste_y = s // 2 - saucer_img.height // 2
    img.paste(saucer_img, (paste_x, paste_y), saucer_img)

    # 4. Анти-алиасинг: downscale
    img = img.resize((size, size), Image.LANCZOS)
    return img


# ──────────────────────────────────────────────
# ICO-упаковка
# ──────────────────────────────────────────────

def make_ico(png_sizes, output_path):
    """
    Создаёт .ico файл из списка (size, PIL.Image) или списка PNG-путей.
    png_sizes: список кортежей (size, image) или (size, filepath)
    """
    images = []
    for entry in png_sizes:
        if isinstance(entry[1], Image.Image):
            images.append(entry[1])
        else:
            img = Image.open(entry[1]).convert('RGBA')
            images.append(img)

    # ICO format: header + directory entries + image data
    num_images = len(images)
    header = struct.pack('<HHH', 0, 1, num_images)

    # Bytes per image (ICO format uses BMP or PNG)
    # We'll use PNG for each entry
    import io
    png_data_list = []
    for img in images:
        buf = io.BytesIO()
        # ICO wants BGRA for BMP, but we can embed PNG directly (Windows Vista+)
        # Actually, embed PNG directly for simplicity
        img.save(buf, format='PNG')
        png_data_list.append(buf.getvalue())

    # Directory entries
    offset = 6 + 16 * num_images  # header + directory
    directory = b''
    for i, img in enumerate(images):
        w = img.width if img.width < 256 else 0
        h = img.height if img.height < 256 else 0
        bpp = 32
        size_bytes = len(png_data_list[i])
        directory += struct.pack('<BBBBHHII', w, h, 0, 0, 1, bpp, size_bytes, offset)
        offset += size_bytes

    with open(output_path, 'wb') as f:
        f.write(header)
        f.write(directory)
        for data in png_data_list:
            f.write(data)

    print(f"  ✓ {output_path}  ({num_images} sizes, {offset} bytes)")


# ──────────────────────────────────────────────
# Генерация всего набора
# ──────────────────────────────────────────────

def generate_all(output_dir='Assets'):
    """Генерирует все варианты иконок."""
    os.makedirs(output_dir, exist_ok=True)
    root_dir = os.path.dirname(os.path.abspath(__file__))

    configs = [
        # (size, detail, label)
        (256, 3, 'large'),
        (48,  2, 'medium'),
        (24,  1, 'tray'),
    ]

    for theme in ('dark', 'light'):
        print(f"\n🎨 Тема: {theme}")
        ico_entries = []
        tray_png = None

        for size, detail, label in configs:
            print(f"  ─ {label} ({size}×{size}, detail={detail}) ...", end=' ', flush=True)
            img = create_icon(size, theme, detail)
            ico_entries.append((size, img))

            if label == 'tray':
                tray_png = img

            # Сохраняем отдельный PNG
            png_name = f'icon_{theme}_{label}.png'
            png_path = os.path.join(output_dir, png_name)
            img.save(png_path, format='PNG')
            print(f'{png_name}')

        # .ico — многослойный (256, 48, 32, 24, 16)
        # Генерируем 32 и 16 из среднего/малого
        medium_48 = create_icon(48, theme, 2)
        small_32 = create_icon(32, theme, 2)
        tiny_16 = create_icon(16, theme, 1)

        ico_full = [
            (256, ico_entries[0][1]),  # large
            (48,  medium_48),
            (32,  small_32),
            (24,  ico_entries[2][1]),  # tray
            (16,  tiny_16),
        ]

        ico_path = os.path.join(root_dir, f'icon_{theme}.ico')
        make_ico(ico_full, ico_path)

    # Также генерируем .ico для проекта (безымянный, dark)
    # Копируем dark как основной
    import shutil
    main_ico = os.path.join(root_dir, 'icon_dark.ico')
    if os.path.exists(main_ico):
        shutil.copy(main_ico, os.path.join(root_dir, 'icon.ico'))
        print(f"\n  ✓ icon.ico (основной, скопирован из dark)")

    # Cleanup отладочных png
    print(f"\n✅ Готово! Иконки в {output_dir}/ и корне проекта.")


if __name__ == '__main__':
    generate_all()
