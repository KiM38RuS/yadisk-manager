"""Analyze v4 icon."""
from PIL import Image
import numpy as np

img = Image.open('output/icon_dark_large.png')
arr = np.array(img)
h, w = arr.shape[:2]
cx = w // 2

print("=== Vertical scan x=128 ===")
for y in range(0, h):
    r, g, b, a = arr[y, cx]
    if a > 8:
        label = "?"
        if a > 200:
            if r > 150 and g > 150 and b > 150:
                label = "BODY"
            elif r < 130 and g > 170 and b > 220:
                label = "DOME"
            elif r > 200 and g > 170 and b < 100:
                label = "LIGHTS"
            else:
                label = "OPQ"
        elif a > 80:
            label = "BEAM-I"
        elif a > 20:
            label = "BEAM-O"
        else:
            label = "BEAM-F"
        print(f"y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}")

print()
# Counts
body = ((arr[:,:,0] > 130) & (arr[:,:,0] < 220) &
        (arr[:,:,1] > 150) & (arr[:,:,1] < 225) &
        (arr[:,:,2] > 160) & (arr[:,:,2] < 220) &
        (np.abs(arr[:,:,0].astype(int) - arr[:,:,2].astype(int)) < 25) &
        (arr[:,:,3] > 200))
print(f"Body silver: {body.sum()}")

dome = ((arr[:,:,0] < 130) & (arr[:,:,1] > 150) & (arr[:,:,2] > 200) & (arr[:,:,3] > 200))
print(f"Dome blue: {dome.sum()}")

lights = ((arr[:,:,0] > 200) & (arr[:,:,1] > 150) & (arr[:,:,2] < 120) & (arr[:,:,3] > 200))
print(f"Lights yellow: {lights.sum()}")

files_y = ((arr[:,:,0] > 220) & (arr[:,:,1] > 170) & (arr[:,:,2] < 130) & (arr[:,:,3] > 50))
files_b = ((arr[:,:,0] < 150) & (arr[:,:,1] > 170) & (arr[:,:,2] > 200) & (arr[:,:,3] > 50)) & ~((arr[:,:,1] > 200) & (arr[:,:,2] > 235))
files_g = ((arr[:,:,1] > 200) & (arr[:,:,0] > 130) & (arr[:,:,0] < 200) & (arr[:,:,2] < 150) & (arr[:,:,3] > 50))
print(f"Yellow files: {((arr[:,:,0] > 220) & (arr[:,:,1] > 170) & (arr[:,:,2] < 130) & (arr[:,:,3] > 50)).sum()}")
print(f"Blue files (non-dome): {files_b.sum()}")
print(f"Green files: {files_g.sum()}")
print(f"Total non-transparent: {(arr[:,:,3] > 0).sum()}/{h*w}")

# Margins
alpha = arr[:,:,3]
for thresh in [1, 50, 100, 200]:
    mask = alpha > thresh
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if rows.any():
        y0, y1 = np.where(rows)[0][[0,-1]]
        x0, x1 = np.where(cols)[0][[0,-1]]
        print(f"alpha>{thresh:3d}: x=[{x0:3d},{x1:3d}] y=[{y0:3d},{y1:3d}]  margins t={y0} b={255-y1} l={x0} r={255-x1}")
