"""Build the project's vector logo, size-tuned PNGs and multi-frame Windows ICO.

Pillow is only a build dependency. The application reads external assets next
to proxy_tool.py, independently of the current working directory. This script
only regenerates assets and never changes the application source.
Run: python assets/build_icons.py
"""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SIZES = (256, 48, 32, 16)
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 64 64">
  <title>LAN Proxy — bidirectional connection</title>
  <defs><linearGradient id="blue" x1="0" y1="0" x2="0.45" y2="1">
    <stop stop-color="#2f75f1"/><stop offset="1" stop-color="#1947bd"/>
  </linearGradient></defs>
  <rect width="64" height="64" rx="16" fill="url(#blue)"/>
  <g fill="none" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M17 24H46 M37 15L46 24L37 33" stroke="#ffffff"/>
    <path d="M47 40H18 M27 31L18 40L27 49" stroke="#95f7e3"/>
  </g>
</svg>
'''


def render(size):
    scale = 8
    edge = size * scale
    mask = Image.new('L', (edge, edge))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, edge - 1, edge - 1), radius=edge / 4, fill=255)
    image = Image.new('RGBA', (edge, edge))
    top, bottom = (47, 117, 241), (25, 71, 189)
    gradient = ImageDraw.Draw(image)
    for y in range(edge):
        for x in range(edge):
            fraction = min(1, (y / (edge - 1) + .45 * x / (edge - 1)) / 1.45)
            color = tuple(round(a + (b - a) * fraction) for a, b in zip(top, bottom))
            gradient.point((x, y), fill=(*color, 255))
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)

    def stroke(points, color, width):
        coordinates = [(round(x * scale), round(y * scale)) for x, y in points]
        thickness = round(width * scale)
        draw.line(coordinates, fill=color, width=thickness, joint='curve')
        radius = thickness / 2
        for x, y in coordinates:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    if size == 16:
        # Wider optical separation and half-pixel centers at the system-tray size.
        stroke([(4, 5.5), (11.25, 5.5)], 'white', 1.5)
        stroke([(9, 3.25), (11.25, 5.5), (9, 7.75)], 'white', 1.5)
        stroke([(12, 10.5), (4.75, 10.5)], '#95f7e3', 1.5)
        stroke([(7, 8.25), (4.75, 10.5), (7, 12.75)], '#95f7e3', 1.5)
    else:
        ratio = size / 64
        for points, color in (
            ([(17, 24), (46, 24)], 'white'),
            ([(37, 15), (46, 24), (37, 33)], 'white'),
            ([(47, 40), (18, 40)], '#95f7e3'),
            ([(27, 31), (18, 40), (27, 49)], '#95f7e3'),
        ):
            stroke([(x * ratio, y * ratio) for x, y in points], color, 6 * ratio)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def build():
    ROOT.joinpath('lan-proxy.svg').write_text(SVG, encoding='utf-8')
    images = {size: render(size) for size in SIZES}
    for size, icon in images.items():
        icon.save(ROOT / f'lan-proxy-{size}.png', optimize=True)
    # Use each independently rendered image, including the optically tuned 16px version.
    images[256].save(ROOT / 'lan-proxy.ico', format='ICO',
                     sizes=[(size, size) for size in SIZES],
                     append_images=[images[size] for size in (48, 32, 16)])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build external application icons in assets/.')
    parser.parse_args()
    build()
