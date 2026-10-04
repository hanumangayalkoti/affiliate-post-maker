"""
watermark.py — Image watermark engine using Pillow.
Bottom-right corner pe semi-transparent dark background with white text.
"""
import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

MARGIN   = 16   # distance from edges
PAD_X    = 12   # horizontal padding inside background
PAD_Y    = 7    # vertical padding inside background
BG_ALPHA = 165  # 0-255 background opacity (165 = ~65%)
RADIUS   = 8    # rounded corner radius


def _get_font(size: int):
    """Try common Linux font paths, fall back to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    # Last resort: built-in default
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def apply_watermark(image_bytes: bytes, text: str) -> bytes:
    """
    Apply a text watermark to image_bytes.
    Returns watermarked JPEG bytes.
    Falls back to original bytes if anything goes wrong.
    """
    if not text:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = img.size

        # Font size: 4% of width, clamped between 20 and 40px
        font_size = max(20, min(40, int(width * 0.04)))
        font = _get_font(font_size)

        # Measure text bounding box
        probe = ImageDraw.Draw(img)
        bbox = probe.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Background rectangle — bottom-right corner
        bg_x1 = width  - text_w - PAD_X * 2 - MARGIN
        bg_y1 = height - text_h - PAD_Y * 2 - MARGIN
        bg_x2 = width  - MARGIN
        bg_y2 = height - MARGIN

        # Draw semi-transparent background on a separate layer
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        ov_draw.rounded_rectangle(
            [bg_x1, bg_y1, bg_x2, bg_y2],
            radius=RADIUS,
            fill=(0, 0, 0, BG_ALPHA),
        )
        img = Image.alpha_composite(img, overlay)

        # Draw white text
        draw = ImageDraw.Draw(img)
        draw.text(
            (bg_x1 + PAD_X, bg_y1 + PAD_Y),
            text,
            font=font,
            fill=(255, 255, 255, 255),
        )

        # Convert to RGB JPEG
        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=92)
        return output.getvalue()

    except Exception as e:
        logger.error(f"Watermark error: {e}")
        return image_bytes  # return original if watermark fails
