"""
ui.py — rangeen buttons. Telegram ab inline button ka rang leta hai
(style: success = hara, primary = neela, danger = laal). python-telegram-bot
20.7 mein ye field nahi hai, isliye api_kwargs se bhejte hain.
"""
from telegram import InlineKeyboardButton

GREEN = "success"
BLUE = "primary"
RED = "danger"

# Post ke neeche wale buttons ke liye user jo rang chun sakta hai
BUTTON_STYLES = {
    "":        "⚪ Normal",
    GREEN:     "🟢 Hara",
    BLUE:      "🔵 Neela",
    RED:       "🔴 Laal",
}


def btn(text: str, style: str = "", **kwargs) -> InlineKeyboardButton:
    """InlineKeyboardButton + rang. style khali = normal button."""
    if style in (GREEN, BLUE, RED):
        kwargs["api_kwargs"] = {**(kwargs.get("api_kwargs") or {}), "style": style}
    return InlineKeyboardButton(text, **kwargs)


def next_style(style: str) -> str:
    keys = list(BUTTON_STYLES)
    try:
        return keys[(keys.index(style or "") + 1) % len(keys)]
    except ValueError:
        return ""
