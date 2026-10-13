"""
Gold Brain AI - chart wallpaper maker (batch-friendly).

Turns your own photos into MetaTrader-ready background BMPs. The ChartWallpaper
indicator then cover-fits each BMP to the exact chart size (auto), so here we
only DIM the photo for readability and cap its resolution. No cropping.

Single:
    python make_wallpaper.py <photo> [out_name] [dim]

    photo     path to your image (jpg/png/webp/...)
    out_name  BMP file name saved in MQL5\\Files (default: chart_wallpaper.bmp)
    dim       0..1 darken (default: AUTO based on brightness; pass a number to force)

Batch (convert many at once):
    python make_wallpaper.py --batch "name1=C:\\a.jpg" "name2=C:\\b.png" ...
    -> saves name1.bmp, name2.bmp ... in MQL5\\Files
"""
import sys, os
from PIL import Image, ImageStat

FILES_DIR = (r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal"
             r"\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files")
MAXSIDE = 1920          # cap the long side (keeps BMP small + indicator fast)
TARGET_LUMA = 72.0      # auto-dim aims the average brightness near this (0..255)


def auto_dim(img):
    """Darker photos need little dimming, bright ones need more."""
    luma = ImageStat.Stat(img.convert("L")).mean[0]
    if luma <= TARGET_LUMA:
        return 0.35
    d = 1.0 - (TARGET_LUMA / luma)
    return max(0.35, min(0.80, d))


def make(photo, out_name="chart_wallpaper.bmp", dim=None):
    if not out_name.lower().endswith(".bmp"):
        out_name += ".bmp"
    out = os.path.join(FILES_DIR, out_name)
    img = Image.open(photo).convert("RGB")
    # cap resolution (keep aspect) - the indicator handles the exact chart fit
    w, h = img.size
    if max(w, h) > MAXSIDE:
        s = MAXSIDE / float(max(w, h))
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    d = auto_dim(img) if dim is None else min(1.0, max(0.0, float(dim)))
    if d > 0:
        img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), d)
    os.makedirs(FILES_DIR, exist_ok=True)
    img.save(out, "BMP")
    print("  %-28s %dx%d  dim %.2f  %d KB" %
          (out_name, img.size[0], img.size[1], d, os.path.getsize(out) // 1024))
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    if sys.argv[1] == "--batch":
        print("Batch wallpaper build:")
        for spec in sys.argv[2:]:
            if "=" not in spec:
                print("  skip (need name=path):", spec); continue
            name, path = spec.split("=", 1)
            try:
                make(path.strip().strip('"'), name.strip())
            except Exception as e:
                print("  ERROR", name, "->", repr(e)[:80])
    else:
        photo = sys.argv[1]
        out_name = sys.argv[2] if len(sys.argv) > 2 else "chart_wallpaper.bmp"
        dim = sys.argv[3] if len(sys.argv) > 3 else None
        make(photo, out_name, dim)
