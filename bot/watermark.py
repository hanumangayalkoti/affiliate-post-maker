"""
watermark.py — normal photo (card ke bina) pe watermark. Drawing card.py mein
hai taaki card aur normal photo dono pe ek jaisa dikhe.
"""
import io
import logging

from PIL import Image

from card import draw_watermark

logger = logging.getLogger(__name__)


def apply_watermark(image_bytes: bytes, wm: dict, font_key: str = "poppins") -> bytes:
    """
    Photo pe watermark lagao (wm = task ki watermark setting).
    Watermarked JPEG bytes wapas. Kuch bhi gadbad ho to original photo hi wapas.
    """
    if not (wm or {}).get("text"):
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        img = draw_watermark(img, wm, font_key)
        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=92)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Watermark error: {e}")
        return image_bytes
