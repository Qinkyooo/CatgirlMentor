"""Convert the approved portrait to ICO and render the native book symbol."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def export(portrait: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    Image.open(portrait).save(target / "desktop.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    frames = []
    for size in (16, 20, 24, 32, 40, 48):
        # Native, flat geometry at each target size; no crop from comparison sheet.
        scale = 4
        image = Image.new("RGBA", (size * scale, size * scale))
        draw = ImageDraw.Draw(image)
        unit = size * scale / 32
        def box(coords):
            return tuple(round(n * unit) for n in coords)
        outline = "#625D50"
        width = max(4, round(1.6 * unit))
        draw.rounded_rectangle(box((4, 2, 28, 30)), radius=round(4 * unit), fill="#ADC98D", outline=outline, width=width)
        if size > 16:
            draw.line(box((8, 4, 8, 24)), fill="#FFF9ED", width=max(3, round(unit)))
        draw.rounded_rectangle(box((4, 23, 28, 30)), radius=round(3 * unit), fill="#FFF9ED", outline=outline, width=width)
        draw.polygon([box(p) for p in ((18, 3), (24, 3), (24, 14), (21, 12), (18, 14))], fill="#F1D88F")
        if size >= 24:
            draw.line(box((10, 27, 27, 27)), fill=outline, width=max(3, round(unit)))
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        image.save(target / f"tray-{size}.png")
        frames.append(image)
    frames[-1].save(target / "tray.ico", append_images=frames[:-1], sizes=[frame.size for frame in frames])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("portrait", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    export(args.portrait, args.target)
