"""Analyze v2 icon pixel data."""
from PIL import Image
import numpy as np

img = Image.open('Assets/icon_dark_large.png')
arr = np.array(img)

h, w = arr.shape[:2]
cx = w // 2

print("=== Vertical scan at center x=128 ===")
for y in range(0, h):
    r, g, b, a = arr[y, cx]
    if a > 10:
        label = "MIX"
        if r > 140 and g > 140 and b > 140 and r > b - 10:
            label = "BODY"
        elif r < 120 and g > 170 and b > 220:
            label = "DOME"
        elif r > 200 and g > 230 and b > 245 and a > 200:
            label = "HIGHLIGHT"
        elif a < 100:
            if r > 210 and g > 245 and b > 250:
                label = "RING"
            else:
                label = "BEAM"
        elif a > 100 and r > 200 and g > 240 and b > 250:
            label = "RAY"
        print(f"y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}")

print()

# Count by category
silver = ((arr[:,:,0] > 110) & (arr[:,:,0] < 215) & 
          (arr[:,:,1] > 130) & (arr[:,:,1] < 220) &
          (arr[:,:,2] > 145) & (arr[:,:,2] < 215) &
          (np.abs(arr[:,:,0].astype(int) - arr[:,:,2].astype(int)) < 30) &
          (arr[:,:,3] > 200))
print(f"BODY silver pixels (opaque): {silver.sum()}")

blue = ((arr[:,:,0] < 130) & (arr[:,:,1] > 150) & (arr[:,:,2] > 200) & (arr[:,:,3] > 200))
print(f"DOME blue pixels (opaque): {blue.sum()}")

beam = (arr[:,:,3] > 5) & (arr[:,:,3] < 130)
print(f"BEAM translucent pixels: {beam.sum()}")

total = (arr[:,:,3] > 0).sum()
print(f"Total non-transparent: {total}/{h*w} ({100*total/h/w:.0f}%)")
