"""Analyze v3 icon."""
from PIL import Image
import numpy as np

img = Image.open('output/icon_dark_large.png')
arr = np.array(img)
h, w = arr.shape[:2]
cx = w // 2

# Vertical center scan
print("=== Vertical scan x=128 ===")
for y in range(0, h):
    r, g, b, a = arr[y, cx]
    if a > 5:
        label = "?"
        if r > 150 and g > 150 and b > 150:
            label = "BODY"
        elif r < 120 and g > 170 and b > 220:
            label = "DOME"
        elif r > 200 and g > 230 and b > 245 and a > 150:
            label = "HIGHLIGHT"
        elif a < 100:
            if r > 210 and g > 245 and b > 250:
                label = "RING"
            else:
                label = "BEAM"
        elif r > 250 and g > 200 and b < 130:
            label = "FILE1"
        elif r < 130 and g > 180 and b > 240 and a > 150:
            label = "DOME"
        print(f"y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}")

print()
# Count categories
body = ((arr[:,:,0] > 130) & (arr[:,:,0] < 220) &
        (arr[:,:,1] > 150) & (arr[:,:,1] < 225) &
        (arr[:,:,2] > 160) & (arr[:,:,2] < 220) &
        (np.abs(arr[:,:,0].astype(int) - arr[:,:,2].astype(int)) < 25) &
        (arr[:,:,3] > 200))
print(f"Body (silver) opaque: {body.sum()}")

dome = ((arr[:,:,0] < 130) & (arr[:,:,1] > 150) & (arr[:,:,2] > 200) & (arr[:,:,3] > 200))
print(f"Dome (blue) opaque: {dome.sum()}")

beam = (arr[:,:,3] > 5) & (arr[:,:,3] < 130)
print(f"Beam translucent: {beam.sum()}")

files = ((arr[:,:,0] > 200) & (arr[:,:,1] > 150) & (arr[:,:,2] < 180) & (arr[:,:,3] > 150))
print(f"Files (yellow-ish): {files.sum()}")

total = (arr[:,:,3] > 0).sum()
print(f"Total non-transparent: {total}/{h*w} ({100*total/h/w:.0f}%)")
