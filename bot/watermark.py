"""
watermark.py — normal photo (card ke bina) pe watermark. Drawing card.py mein
hai taaki card aur normal photo dono pe ek jaisa dikhe.
"""
import io
import logging

from PIL import Image

from card import draw_watermark, draw_amazon_badge, badge_box

logger = logging.getLogger(__name__)


def apply_watermark(image_bytes: bytes, wm: dict, font_key: str = "poppins",
                    badge: bool = False) -> bytes:
    """
    Photo pe watermark (wm = task ki setting) aur chahe to Amazon badge lagao.
    JPEG bytes wapas. Kuch bhi gadbad ho to original photo hi wapas.
    """
    has_wm = bool((wm or {}).get("enabled", True) and (wm or {}).get("text"))
    if not has_wm and not badge:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        if badge and min(img.size) < 400:
            # chhoti photo pe badge bahut bada dikhega — pehle thoda bada karo
            k = 400 / min(img.size)
            img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
        box = badge_box(*img.size) if badge else None
        if has_wm:
            img = draw_watermark(img, wm, font_key, avoid_left=(box[0] + box[2]) if box else 0)
        if box:
            img = draw_amazon_badge(img)
        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=92)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Watermark error: {e}")
        return image_bytes
