"""
broadcasts.py — admin broadcast: kisko bhejna (segment / chune hue users),
chalte waqt live progress, aur 48 ghante tak "Recall" (bheja hua message wapas).

Har bheje gaye message ka (chat_id, msg_id) database mein yaad rehta hai —
bot restart ho jaaye tab bhi recall chalega. Telegram bot ko apna message
sirf 48 ghante tak delete karne deta hai, isliye recall bhi 48 ghante tak.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, BadRequest

from storage import get_db

logger = logging.getLogger(__name__)

RECALL_HOURS = 48
_running = {"on": False}


def is_running() -> bool:
    return _running["on"]


# =============================================================================
# DATABASE
# =============================================================================
def init_tables():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS broadcasts (
                        id          BIGSERIAL PRIMARY KEY,
                        admin_id    BIGINT    NOT NULL,
                        audience    TEXT      NOT NULL,
                        total       INTEGER   NOT NULL DEFAULT 0,
                        sent        INTEGER   NOT NULL DEFAULT 0,
                        failed      INTEGER   NOT NULL DEFAULT 0,
                        blocked     INTEGER   NOT NULL DEFAULT 0,
                        recalled    BOOLEAN   NOT NULL DEFAULT FALSE,
                        created_at  TIMESTAMP NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS broadcast_msgs (
                        broadcast_id BIGINT NOT NULL,
                        chat_id      BIGINT NOT NULL,
                        msg_id       BIGINT NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS broadcast_msgs_bc_idx ON broadcast_msgs (broadcast_id)")
    except Exception as e:
        logger.error(f"broadcast tables error: {e}")


def _create(admin_id: int, audience: str, total: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO broadcasts (admin_id, audience, total) VALUES (%s, %s, %s) RETURNING id",
                            (admin_id, audience, total))
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"broadcast create error: {e}")
        return None


def _save_msgs(bc_id, rows: list):
    if not bc_id or not rows:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO broadcast_msgs (broadcast_id, chat_id, msg_id) VALUES (%s, %s, %s)",
                                [(bc_id, c, m) for c, m in rows])
    except Exception as e:
        logger.error(f"broadcast msgs save error: {e}")


def _finish(bc_id, sent: int, failed: int, blocked: int):
    if not bc_id:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE broadcasts SET sent = %s, failed = %s, blocked = %s WHERE id = %s",
                            (sent, failed, blocked, bc_id))
    except Exception as e:
        logger.error(f"broadcast finish error: {e}")


def recent(limit: int = 10) -> list:
    """Pichhle 48 ghante ke broadcast jo abhi recall ho sakte hain."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, audience, total, sent, created_at FROM broadcasts
                       WHERE recalled = FALSE AND created_at > %s ORDER BY id DESC LIMIT %s""",
                    (datetime.utcnow() - timedelta(hours=RECALL_HOURS), limit))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"broadcast recent error: {e}")
        return []


def get(bc_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, audience, total, sent, created_at, recalled FROM broadcasts WHERE id = %s",
                            (bc_id,))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"broadcast get error: {e}")
        return None


def _msgs(bc_id: int) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, msg_id FROM broadcast_msgs WHERE broadcast_id = %s", (bc_id,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"broadcast msgs error: {e}")
        return []


def _mark_recalled(bc_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE broadcasts SET recalled = TRUE WHERE id = %s", (bc_id,))
                cur.execute("DELETE FROM broadcast_msgs WHERE broadcast_id = %s", (bc_id,))
    except Exception as e:
        logger.error(f"broadcast recall mark error: {e}")


def prune():
    """48 ghante se purane message IDs ka koi kaam nahi (recall ho hi nahi sakta)."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM broadcast_msgs WHERE broadcast_id IN
                       (SELECT id FROM broadcasts WHERE created_at < %s)""",
                    (datetime.utcnow() - timedelta(hours=RECALL_HOURS + 1),))
    except Exception as e:
        logger.error(f"broadcast prune error: {e}")


# =============================================================================
# SEND / RECALL
# =============================================================================
async def run(bot, admin_uid: int, from_chat: int, msg_id: int, ids: list, audience: str,
              status_msg=None, mark_bot_blocked=None, recall_kb=None):
    """Sabko copy karo. status_msg ho to har ~25 user pe progress edit hoti hai."""
    bc_id = _create(admin_uid, audience, len(ids))
    ok = fail = blocked = 0
    delivered = []
    _running["on"] = True

    async def progress(final=False):
        if status_msg is None and not final:
            return
        done = ok + fail + blocked
        head = "✅ <b>Broadcast poora</b>" if final else "📣 <b>Broadcast chal raha hai...</b>"
        text = (f"{head}\n\n👥 {done}/{len(ids)}\n✅ Pahuncha: {ok}\n🚫 Bot block: {blocked}\n❌ Fail: {fail}"
                + (f"\n\n↩️ {RECALL_HOURS} ghante tak wapas le sakte hain." if final and ok and bc_id else ""))
        kb = recall_kb(bc_id) if final and ok and bc_id and recall_kb else None
        try:
            if status_msg is None:
                raise RuntimeError("no status message")
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            # Status message hat gaya (chat clean) — aakhri summary naye message mein
            if final and "not modified" not in str(e).lower():
                try:
                    await bot.send_message(admin_uid, text, parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception:
                    pass

    try:
        for i, target in enumerate(ids):
            for attempt in range(2):
                try:
                    sent = await bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=msg_id)
                    ok += 1
                    delivered.append((target, getattr(sent, "message_id", 0)))
                    break
                except RetryAfter as e:
                    if attempt:
                        fail += 1
                        break
                    await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
                except Forbidden:
                    blocked += 1
                    if mark_bot_blocked:
                        mark_bot_blocked(target)
                    break
                except Exception:
                    fail += 1
                    break
            await asyncio.sleep(0.05)
            if len(delivered) >= 50:
                _save_msgs(bc_id, delivered)
                delivered = []
            if i and i % 25 == 0:
                await progress()
    finally:
        _save_msgs(bc_id, delivered)
        _finish(bc_id, ok, fail, blocked)
        _running["on"] = False
    await progress(final=True)
    return bc_id, ok, fail, blocked


async def recall(bot, bc_id: int):
    """Broadcast ke saare message delete. Returns (deleted, failed)."""
    rows = _msgs(bc_id)
    deleted = failed = 0
    for chat_id, msg_id in rows:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                deleted += 1
            except Exception:
                failed += 1
        except (BadRequest, Forbidden, Exception):
            failed += 1
        await asyncio.sleep(0.04)
    _mark_recalled(bc_id)
    return deleted, failed
