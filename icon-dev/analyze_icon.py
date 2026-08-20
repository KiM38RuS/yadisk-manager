"""Analyze generated icon pixel data."""
from PIL import Image
import numpy as np

img = Image.open('Assets/icon_dark_large.png')
arr = np.array(img)

h, w = arr.shape[:2]
cx = w // 2

print("=== Vertical scan at x=128 (center) ===")
for y in range(0, h):
    r, g, b, a = arr[y, cx]
    if a > 10:
        label = "MIXED"
        if r > 150 and g > 150 and b > 150 and r > b:
            label = "BODY-SILVER"
        elif r < 130 and g > 170 and b > 220:
            label = "DOME-BLUE"
        elif r > 200 and g > 235 and b > 250 and a > 200:
            label = "HIGHLIGHT"
        elif a < 100:
            label = "BEAM"
        elif a > 100 and r > 210 and g > 240 and b > 250:
            label = "BEAM-RAY"
        print(f"y={y:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}")

print()
print("=== Horizontal scan at y = 144 (body expected) ===")
y_body = 144
for x in range(0, w):
    r, g, b, a = arr[y_body, x]
    if a > 10:
        label = "?"
        if r > 150 and g > 150 and b > 150:
            label = "SILVER"
        elif r < 130 and g > 170 and b > 220:
            label = "DOME"
        elif a < 100:
            label = "BEAM"
        print(f"x={x:3d} ({r:3d},{g:3d},{b:3d},{a:3d}) {label}")

print()
# Count body-colored pixels
silver = ((arr[:,:,0] > 110) & (arr[:,:,0] < 210) & 
          (arr[:,:,1] > 130) & (arr[:,:,1] < 220) &
          (arr[:,:,2] > 145) & (arr[:,:,2] < 215) &
          (np.abs(arr[:,:,0].astype(int) - arr[:,:,2].astype(int)) < 25) &
          (arr[:,:,3] > 200))
print(f"Silvery body pixels (opaque): {silver.sum()}")

blue = ((arr[:,:,0] < 140) & (arr[:,:,1] > 160) & (arr[:,:,2] > 210) & (arr[:,:,3] > 200))
print(f"Blue dome pixels (opaque): {blue.sum()}")

# Save debug visualization
debug = np.zeros((h, w, 3), dtype=np.uint8)
debug[silver] = (255, 255, 255)
debug[blue] = (0, 100, 255)
beam = (arr[:,:,3] > 5) & (arr[:,:,3] < 130)
debug[beam] = (50, 200, 50)
bg = arr[:,:,3] == 0
debug[bg] = (40, 40, 40)
Image.fromarray(debug).save('Assets/debug_regions.png')
print("\nDebug saved")
