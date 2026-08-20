"""Analyze v5."""
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
            label = 'DISC'
        elif r < 130 and g > 170 and b > 220 and a > 200:
            label = 'DOME'
        elif a < 100:
            label = 'BEAM'
        elif r > 200 and g > 150 and b < 130 and a > 100:
            label = 'FILE'
        else:
            label = 'SHADOW'
        if label != prev:
            print(f'  y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}')
        prev = label

print()

body = (arr[:,:,0] > 130) & (arr[:,:,1] > 150) & (arr[:,:,2] > 150) & \
       (arr[:,:,0] < 230) & (arr[:,:,1] < 235) & (arr[:,:,2] < 240) & \
       (arr[:,:,3] > 200)
dome = (arr[:,:,0] < 130) & (arr[:,:,1] > 170) & (arr[:,:,2] > 220) & (arr[:,:,3] > 200)
beam = (arr[:,:,3] > 10) & (arr[:,:,3] < 100)
files_y = (arr[:,:,0] > 220) & (arr[:,:,1] > 150) & (arr[:,:,2] < 130) & (arr[:,:,3] > 80)
files_g = (arr[:,:,1] > 200) & (arr[:,:,0] > 130) & (arr[:,:,0] < 200) & (arr[:,:,2] < 150) & (arr[:,:,3] > 80)
files_b = (arr[:,:,0] < 150) & (arr[:,:,1] > 170) & (arr[:,:,2] > 200) & (arr[:,:,3] > 80) & ~dome
print(f'Disc: {body.sum()}, Dome: {dome.sum()}')
print(f'Files Y: {files_y.sum()}, G: {files_g.sum()}, B: {files_b.sum()}')
print(f'Beam: {beam.sum()}')
total = (arr[:,:,3] > 0).sum()
print(f'Total: {total}/{h*w}')

alpha = arr[:,:,3]
for t in [1, 10, 50, 100, 200]:
    mask = alpha > t
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any():
        y0, y1 = np.where(rows)[0][[0,-1]]
        x0, x1 = np.where(cols)[0][[0,-1]]
        print(f'alpha>{t:3d}: x=[{x0:3d},{x1:3d}] y=[{y0:3d},{y1:3d}]  mar t={y0} b={255-y1} l={x0} r={255-x1}')
