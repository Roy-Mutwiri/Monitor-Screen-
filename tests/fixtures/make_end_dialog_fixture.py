"""Generate the sanitized end-stream dialog fixture (no private screenshot is committed).
Run: python tests/fixtures/make_end_dialog_fixture.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def make(path: Path, width: int = 860, height: int = 480) -> Path:
    img = Image.new("RGB", (width, height), (16, 16, 18))
    d = ImageDraw.Draw(img)
    # a bit of "studio" background so the dialog is not the whole frame
    for x in range(0, width, 40):
        d.line((x, 0, x + 20, 60), fill=(60, 30, 30))
    d.text((width - 140, 10), "4,142,297", fill=(230, 230, 230))
    box = (80, 70, 780, 400)
    d.rounded_rectangle(box, radius=14, fill=(37, 37, 40))
    try:
        big = ImageFont.truetype("segoeuib.ttf", 34)
        body = ImageFont.truetype("segoeui.ttf", 26)
        btn = ImageFont.truetype("segoeuib.ttf", 26)
    except OSError:
        big = body = btn = ImageFont.load_default()
    d.text((130, 120), "End streaming?", font=big, fill=(245, 245, 245))
    d.text((130, 190), "End LIVE? Share your LIVE for more viewers.", font=body, fill=(225, 225, 225))
    d.rounded_rectangle((130, 280, 420, 350), radius=8, fill=(230, 38, 83))
    d.text((228, 298), "End now", font=btn, fill=(255, 255, 255))
    d.rounded_rectangle((440, 280, 730, 350), radius=8, fill=(58, 58, 62))
    d.text((545, 298), "Cancel", font=btn, fill=(240, 240, 240))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


if __name__ == "__main__":
    print(make(Path(__file__).with_name("end_streaming_dialog_synthetic.png")))
