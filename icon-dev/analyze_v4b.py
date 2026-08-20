"""Analyze v4 icon output."""
from PIL import Image
import numpy as np

img = Image.open('output/icon_dark_large.png')
arr = np.array(img)
h, w = arr.shape[:2]
cx = w // 2

print('=== Vertical scan ===')
prev = ''
for y in range(0, h):
    r, g, b, a = arr[y, cx]
    if a > 15:
        if r > 150 and g > 150 and b > 150 and a > 200:
            label = 'LIGHT'
        elif r < 130 and g > 170 and b > 220 and a > 200:
            label = 'DOME'
        elif a < 100:
            label = 'BEAM'
        elif r > 200 and g > 170 and b < 120:
            label = 'FILE'
        else:
            label = 'DARK'
        if label != prev:
            print(f'  y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}')
        prev = label

print()

body = (arr[:,:,0] > 130) & (arr[:,:,1] > 150) & (arr[:,:,2] > 150) & \
       (arr[:,:,0] < 220) & (arr[:,:,1] < 230) & (arr[:,:,2] < 230) & \
       (arr[:,:,3] > 200) & (np.abs(arr[:,:,0].astype(int) - arr[:,:,2].astype(int)) < 30)
dome = (arr[:,:,0] < 130) & (arr[:,:,1] > 170) & (arr[:,:,2] > 220) & (arr[:,:,3] > 200)
files = (arr[:,:,0] > 200) & (arr[:,:,1] > 150) & (arr[:,:,2] < 140) & (arr[:,:,3] > 50)
print(f'Body: {body.sum()}, Dome: {dome.sum()}, Files: {files.sum()}')
total = (arr[:,:,3] > 0).sum()
print(f'Total: {total}')

alpha = arr[:,:,3]
for t in [1, 50, 100, 200]:
    mask = alpha > t
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any():
        y0, y1 = np.where(rows)[0][[0,-1]]
        x0, x1 = np.where(cols)[0][[0,-1]]
        print(f'alpha>{t:3d}: x=[{x0:3d},{x1:3d}] y=[{y0:3d},{y1:3d}]')
