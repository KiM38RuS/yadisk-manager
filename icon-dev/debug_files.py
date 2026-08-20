"""Debug file visibility in v5."""
from PIL import Image
import numpy as np

img = Image.open('output/icon_dark_large.png')
arr = np.array(img)
h, w = arr.shape[:2]

# Expected file locations based on file_data
cs = 1126  # from create_icon
cx = cs // 2
by1 = 676  # calculated in script but approximate
print(f'cs={cs}, cx={cx}, by1={by1}')

file_positions = [
    (-0.08, 0.06),
    ( 0.08, 0.03),
    (-0.16, 0.20),
    ( 0.16, 0.15),
    ( 0.00, 0.10),
    ( 0.10, 0.35),
]
for oxr, dyr in file_positions:
    fx = cx + int(oxr * cs)
    fy = int(by1 + dyr * cs)
    # Scale to 256x256
    sx = int(fx * 256 / cs)
    sy = int(fy * 256 / cs)
    print(f'  File at canvas({fx},{fy}) → image({sx},{sy}): RGB={arr[sy,sx,:3]}, A={arr[sy,sx,3]}')

# Scan a broader area around expected locations
for oxr, dyr in file_positions:
    fx = cx + int(oxr * cs)
    fy = int(by1 + dyr * cs)
    sx = int(fx * 256 / cs)
    sy = int(fy * 256 / cs)
    # Check 5x5 neighborhood
    y0, y1 = max(0, sy-2), min(h, sy+3)
    x0, x1 = max(0, sx-2), min(w, sx+3)
    patch = arr[y0:y1, x0:x1]
    bright = (patch[:,:,3] > 50) & ((patch[:,:,0] > 200) | (patch[:,:,1] > 200) | (patch[:,:,2] > 200))
    print(f'  Area ({sx},{sy}) bright pixels: {bright.sum()}/{patch.shape[0]*patch.shape[1]}')
