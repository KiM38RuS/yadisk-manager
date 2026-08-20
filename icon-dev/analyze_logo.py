"""Analyze Logo 1.png reference image."""
from PIL import Image
import numpy as np

img = Image.open('../Reference/Logo 1.png')
arr = np.array(img)
h, w = arr.shape[:2]

print(f"Size: {w}x{h}")
print(f"Mode: {img.mode}")

# Look at specific y-levels
print("\n=== Horizontal scans at key y positions ===")
for y in [0, 50, 100, 150, 200, 250, 300, 350, 355, 358, 400, 450, 507]:
    row = arr[y, :, :]
    alpha = row[:, 3]
    trans = alpha > 10
    if trans.any():
        x_vals = np.where(trans)[0]
        first, last = int(x_vals[0]), int(x_vals[-1])
        c_first = tuple(row[first])
        c_last = tuple(row[last])
        colors = row[trans][:, :3].astype(int)
        mean_c = tuple(colors.mean(axis=0))
        print(f"\ny={y}: x=[{first},{last}], mean=({mean_c[0]},{mean_c[1]},{mean_c[2]})")
        for x in [first, first + (last-first)//3, (first+last)//2, last - (last-first)//3, last]:
            x = int(x)
            if x < w:
                c = arr[y, x]
                print(f"  x={x}: ({c[0]},{c[1]},{c[2]},{c[3]})")

print("\n=== Trying to identify the logo shape ===")
# Only non-black pixels with high alpha
bright = (arr[:,:,0] > 50) & (arr[:,:,1] > 50) & (arr[:,:,2] > 50) & (arr[:,:,3] > 100)
print(f"Bright foreground pixels: {bright.sum()}")

# Blue-ish pixels
blueish = (arr[:,:,2] > arr[:,:,0]) & (arr[:,:,2] > arr[:,:,1]) & (arr[:,:,3] > 100)
print(f"Blue-ish pixels: {blueish.sum()}")

white = (arr[:,:,0] > 200) & (arr[:,:,1] > 200) & (arr[:,:,2] > 200) & (arr[:,:,3] > 100)
print(f"White/light pixels: {white.sum()}")

# Save a debug version showing only bright content
debug = np.zeros((h, w, 3), dtype=np.uint8)
debug[bright] = (255, 255, 255)
debug[(~bright) & (arr[:,:,3] > 10)] = (100, 100, 100)
Image.fromarray(debug).save('debug_logo_reference.png')
print("\nDebug image saved")
