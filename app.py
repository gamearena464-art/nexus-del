
import os
import re
import html
import time
import asyncio
import sqlite3
import logging
import tempfile
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask
from groq import Groq
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================================================
# NEXUS KI-ASSISTENT
# Render Start Command: gunicorn app:app
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("NEXUS")

# Flask muss genau app heißen!
app = Flask(__name__)


@app.route("/")
def home():
    return "NEXUS KI-Assistent läuft! 🤖", 200


@app.route("/health")
def health():
    return "OK", 200


# Einstellungen aus Render
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
).strip()
WHISPER_MODEL = os.getenv(
    "WHISPER_MODEL", "whisper-large-v3"
).strip()
EDGE_TTS_VOICE = os.getenv(
    "EDGE_TTS_VOICE", "de-DE-ConradNeural"
).strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-2.5-flash"
).strip()

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/nexus_memory.sqlite3"
    if Path("/var/data").exists()
    else "nexus_memory.sqlite3",
)

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """
Du bist NEXUS, ein freundlicher deutscher KI-Assistent.
Antworte kurz, einfach und verständlich.
Erkläre Dinge so, dass auch ein vierjähriges Kind sie versteht.
Nutze passende Emojis, aber nicht zu viele.
Sei freundlich, ruhig und respektvoll.
Erfinde keine Fakten.
Wenn du gefragt wirst, wer dich programmiert hat,
antworte: Der liebe Heiko aus dem Schwabenländle. 😊
""".strip()

histories = defaultdict(list)
group_active_until = {}
MAX_HISTORY = 12
GROUP_ACTIVE_SECONDS = 180


# ---------------- Datenbank / Gedächtnis ----------------

def db():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_db():
    with db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                chat_id TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, memory_key)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS conversation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def save_memory(chat_id, key, value):
    try:
        with db() as connection:
            connection.execute("""
                INSERT INTO memories
                (chat_id, memory_key, memory_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, memory_key)
                DO UPDATE SET
                    memory_value=excluded.memory_value,
                    updated_at=excluded.updated_at
            """, (
                str(chat_id),
                key[:100],
                value[:1000],
                datetime.now(timezone.utc).isoformat(),
            ))
        return True
    except Exception:
        logger.exception("Gedächtnis konnte nicht gespeichert werden")
        return False


def get_memories(chat_id):
    try:
        with db() as connection:
            return connection.execute("""
                SELECT memory_key, memory_value
                FROM memories
                WHERE chat_id=?
                ORDER BY updated_at DESC
                LIMIT 30
            """, (str(chat_id),)).fetchall()
    except Exception:
        logger.exception("Gedächtnis konnte nicht gelesen werden")
        return []


def forget_memory(chat_id, search):
    with db() as connection:
        cursor = connection.execute("""
            DELETE FROM memories
            WHERE chat_id=? AND memory_key LIKE ?
        """, (str(chat_id), f"%{search}%"))
        return cursor.rowcount


def log_message(chat_id, role, content):
    try:
        with db() as connection:
            connection.execute("""
                INSERT INTO conversation_log
                (chat_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                str(chat_id),
                role,
                content[:4000],
                datetime.now(timezone.utc).isoformat(),
            ))
    except Exception:
        logger.exception("Nachrichtenprotokoll fehlgeschlagen")


def learn_from_text(chat_id, text):
    patterns = [
        (r"\bich heiße\s+(.+)", "Name"),
        (r"\bmein name ist\s+(.+)", "Name"),
        (r"\bich mag\s+(.+)", "Mag"),
        (r"\bich liebe\s+(.+)", "Mag"),
        (r"\bmein hobby ist\s+(.+)", "Hobby"),
        (r"\bmein lieblingsessen ist\s+(.+)", "Lieblingsessen"),
        (r"\bmerk dir\s+(.+)", "Notiz"),
        (r"\bmerke dir\s+(.+)", "Notiz"),
    ]

    for pattern, key in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .!?")
            if value and save_memory(chat_id, key, value):
                return f"Alles klar! 🧠 Ich merke mir: {key}: {value}"

    return None


# ---------------- KI-Antworten ----------------

def get_ai_reply(chat_id, user_text):
    if not client:
        return "Meine KI ist noch nicht verbunden. Bitte prüfe GROQ_API_KEY in Render."

    memories = get_memories(chat_id)
    memory_text = "\n".join(
        f"- {key}: {value}" for key, value in memories
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if memory_text:
        messages.append({
            "role": "system",
            "content": "Gespeicherte Erinnerungen:\n" + memory_text,
        })

    messages.extend(histories[str(chat_id)][-MAX_HISTORY:])
    messages.append({"role": "user", "content": user_text})

    try:
        result = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=400,
        )

        answer = (
            result.choices[0].message.content
            or "Ich habe gerade keine Antwort."
        )

        histories[str(chat_id)].extend([
            {"role": "user", "content": user_text[:3000]},
            {"role": "assistant", "content": answer[:3000]},
        ])
        histories[str(chat_id)] = histories[str(chat_id)][-MAX_HISTORY:]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen")
        return "Entschuldigung! Meine KI hat gerade ein Problem. Prüfe bitte GROQ_API_KEY und GROQ_MODEL."


# ---------------- Telegram-Befehle ----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Hallo! Ich bin NEXUS. 🤖❤️\n\n"
        "Du kannst mit mir schreiben oder mir eine Sprachnachricht senden.\n\n"
        "/merken Text – Erinnerung speichern\n"
        "/gedaechtnis – Erinnerungen ansehen\n"
        "/vergiss Text – Erinnerung löschen\n"
        "/reset – kurzen Chat-Verlauf löschen\n"
        "/bild Beschreibung – Bild erstellen"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    histories.pop(str(update.effective_chat.id), None)
    await update.effective_message.reply_text(
        "Der kurze Chat-Verlauf wurde zurückgesetzt. 🧹 "
        "Gespeicherte Erinnerungen bleiben erhalten."
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Beispiel: /merken Hobby: Wandern"
        )
        return

    key = "Notiz"
    if ":" in value:
        key, value = value.split(":", 1)
        key, value = key.strip(), value.strip()

    if save_memory(update.effective_chat.id, key, value):
        await update.effective_message.reply_text(
            f"Gespeichert! 🧠\n{key}: {value}"
        )
    else:
        await update.effective_message.reply_text(
            "Ich konnte die Erinnerung nicht speichern."
        )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memories = get_memories(update.effective_chat.id)

    if not memories:
        await update.effective_message.reply_text(
            "Ich habe noch keine Erinnerungen gespeichert. 🧠"
        )
        return

    result = "🧠 Meine Erinnerungen:\n" + "\n".join(
        f"• {key}: {value}" for key, value in memories
    )

    for start in range(0, len(result), 4000):
        await update.effective_message.reply_text(result[start:start + 4000])


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args).strip()

    if not search:
        await update.effective_message.reply_text(
            "Beispiel: /vergiss Hobby"
        )
        return

    count = forget_memory(update.effective_chat.id, search)
    if count:
        await update.effective_message.reply_text("Erinnerung gelöscht. 🧹")
    else:
        await update.effective_message.reply_text(
            "Ich habe keine passende Erinnerung gefunden."
        )


# ---------------- Bilder erstellen ----------------

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()

    if not prompt:
        await update.effective_message.reply_text(
            "Beispiel: /bild ein leuchtender Energieplanet im Weltall"
        )
        return

    await update.effective_message.reply_text(
        "Ich versuche, dein Bild zu erstellen. 🎨"
    )

    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            + requests.utils.quote(prompt, safe="")
        )
        response = await asyncio.to_thread(
            requests.get,
            url,
            params={"width": 1024, "height": 1024, "nologo": "true"},
            timeout=90,
        )
        response.raise_for_status()

        if "image" not in response.headers.get("content-type", ""):
            raise RuntimeError("Der Dienst hat kein Bild zurückgegeben.")

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(response.content)
            path = tmp.name

        try:
            with open(path, "rb") as photo:
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=prompt[:900],
                )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen")
        await update.effective_message.reply_text(
            "Das Bild konnte gerade nicht erstellt werden. Versuche es später erneut."
        )


# ---------------- Sprache verstehen und sprechen ----------------

async def transcribe_voice(path):
    if not client:
        return None

    def transcribe():
        with open(path, "rb") as audio:
            result = client.audio.transcriptions.create(
                file=(os.path.basename(path), audio.read()),
                model=WHISPER_MODEL,
                response_format="text",
                language="de",
            )
        if isinstance(result, str):
            return result.strip()
        return getattr(result, "text", "").strip()

    return await asyncio.to_thread(transcribe)


async def make_voice_file(text, path):
    try:
        import edge_tts
        speaker = edge_tts.Communicate(
            text[:3000],
            EDGE_TTS_VOICE,
        )
        await speaker.save(path)
        return True
    except Exception:
        logger.exception("Sprachausgabe fehlgeschlagen")
        return False


async def send_reply(message, text, voice=False):
    if voice:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            voice_path = tmp.name

        try:
            if await make_voice_file(text, voice_path):
                with open(voice_path, "rb") as audio:
                    await message.reply_voice(voice=audio)
                return
        finally:
            try:
                os.remove(voice_path)
            except OSError:
                pass

    for start in range(0, len(text), 4000):
        await message.reply_text(text[start:start + 4000])


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.voice:
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if time.monotonic() >= group_active_until.get(chat.id, 0):
            await message.reply_text(
                "Sprich mich bitte zuerst mit „Nexus“ an. 😊"
            )
            return

    if not client:
        await message.reply_text("Bitte verbinde zuerst GROQ_API_KEY in Render.")
        return

    await message.reply_text("Ich höre zu … 🎧")
    path = None

    try:
        telegram_file = await context.bot.get_file(message.voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            path = tmp.name

        await telegram_file.download_to_drive(path)
        spoken_text = await transcribe_voice(path)

        if not spoken_text:
            await message.reply_text(
                "Ich konnte dich leider nicht verstehen. Versuch es bitte noch einmal."
            )
            return

        learned = learn_from_text(chat.id, spoken_text)
        if learned:
            await message.reply_text(learned)
            return

        answer = await asyncio.to_thread(get_ai_reply, chat.id, spoken_text)
        await send_reply(message, answer, voice=True)

    except Exception:
        logger.exception("Sprachnachricht fehlgeschlagen")
        await message.reply_text(
            "Beim Verarbeiten deiner Sprachnachricht ist ein Fehler aufgetreten."
        )
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------- Fotos analysieren ----------------

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.photo:
        return

    if not GEMINI_API_KEY:
        await message.reply_text(
            "Für Fotoanalyse fehlt GEMINI_API_KEY in Render."
        )
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if time.monotonic() >= group_active_until.get(chat.id, 0):
            caption = message.caption or ""
            if not is_addressed(caption, context.bot.username or ""):
                return

    path = None

    try:
        from google import genai

        gemini = genai.Client(api_key=GEMINI_API_KEY)
        telegram_file = await context.bot.get_file(message.photo[-1].file_id)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            path = tmp.name

        await telegram_file.download_to_drive(path)

        with open(path, "rb") as photo:
            image_bytes = photo.read()

        caption = message.caption or (
            "Beschreibe dieses Bild auf Deutsch in einfachen Worten."
        )

        result = await asyncio.to_thread(
            lambda: gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    caption,
                    {"mime_type": "image/jpeg", "data": image_bytes},
                ],
            )
        )

        await send_reply(
            message,
            getattr(result, "text", "") or "Ich konnte das Bild nicht beschreiben.",
        )

    except Exception:
        logger.exception("Fotoanalyse fehlgeschlagen")
        await message.reply_text(
            "Die Fotoanalyse hat nicht funktioniert. Prüfe GEMINI_API_KEY."
        )
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------- Gruppenmodus ----------------

def is_addressed(text, username=""):
    text = (text or "").lower()
    username = (username or "").lower().lstrip("@")

    if username and f"@{username}" in text:
        return True

    return bool(re.search(
        r"\b(nexus|ki-assistent|ki assistent|bot)\b",
        text,
    ))


def is_stop_phrase(text):
    text = (text or "").lower().strip()
    return any(phrase in text for phrase in (
        "nexus aus",
        "nexus stopp",
        "stopp nexus",
        "ruhe nexus",
        "tschüss nexus",
        "auf wiedersehen nexus",
        "geh offline",
        "sei still nexus",
        "nexus schlaf",
    ))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text:
        return

    text = message.text.strip()
    group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    now = time.monotonic()

    if group:
        addressed = is_addressed(text, context.bot.username or "")
        active = now < group_active_until.get(chat.id, 0)

        if is_stop_phrase(text) and (addressed or active):
            group_active_until.pop(chat.id, None)
            await message.reply_text("Alles klar, ich bin jetzt ruhig. 👋")
            return

        if addressed:
            group_active_until[chat.id] = now + GROUP_ACTIVE_SECONDS
        elif not active:
            return

    learned = learn_from_text(chat.id, text)
    if learned:
        await message.reply_text(learned)
        return

    answer = await asyncio.to_thread(get_ai_reply, chat.id, text)
    await send_reply(message, answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Telegram-Fehler: %s",
        context.error,
        exc_info=context.error,
    )


# ---------------- Bot starten ----------------

def run_bot():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN fehlt in Render.")
        return

    init_db()

    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("reset", reset_command))
    telegram_app.add_handler(CommandHandler("merken", remember_command))
    telegram_app.add_handler(CommandHandler("gedaechtnis", memories_command))
    telegram_app.add_handler(CommandHandler("vergiss", forget_command))
    telegram_app.add_handler(CommandHandler("bild", image_command))

    telegram_app.add_handler(
        MessageHandler(filters.VOICE, voice_handler)
    )
    telegram_app.add_handler(
        MessageHandler(filters.PHOTO, photo_handler)
    )
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    telegram_app.add_error_handler(error_handler)

    logger.info("NEXUS Telegram-Bot startet.")
    telegram_app.run_polling(drop_pending_updates=False)


def bot_thread_target():
    try:
        run_bot()
    except Exception:
        logger.exception("Telegram-Bot konnte nicht gestartet werden")


_bot_thread = None


def start_bot_thread():
    global _bot_thread

    if not TELEGRAM_TOKEN:
        logger.warning("Telegram-Bot bleibt aus: TELEGRAM_TOKEN fehlt.")
        return

    if _bot_thread and _bot_thread.is_alive():
        return

    _bot_thread = threading.Thread(
        target=bot_thread_target,
        name="nexus-telegram",
        daemon=True,
    )
    _bot_thread.start()


# Gunicorn importiert app.py. Flask bleibt erreichbar,
# während der Telegram-Bot in einem eigenen Thread läuft.
start_bot_thread()
