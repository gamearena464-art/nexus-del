import os
import re
import html
import logging
import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

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

# ============================================================
# NEXUS KI-ASSISTENT – Render + Telegram
# ============================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("nexus")

app = Flask(__name__)


@app.get("/")
def home():
    return "NEXUS KI-Assistent läuft! 🤖", 200


@app.get("/health")
def health():
    return {
        "status": "ok",
        "telegram_token_configured": bool(TELEGRAM_TOKEN),
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
    }, 200


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

MAX_HISTORY_MESSAGES = 12
GROUP_ACTIVE_SECONDS = 180
MAX_TELEGRAM_LENGTH = 3800

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

chat_history = defaultdict(list)
group_active_until = {}

SYSTEM_PROMPT = """
Du bist NEXUS, ein freundlicher deutscher KI-Assistent.

ANTWORTSTIL:
- Antworte auf Deutsch, freundlich und verständlich.
- Erkläre Dinge einfach, aber nicht unnötig kindlich.
- Nutze passende Emojis, jedoch nicht in jedem Satz.
- Verwende klare Überschriften und Absätze.
- Setze nach Überschriften eine Leerzeile.
- Trenne unterschiedliche Themen durch Leerzeilen.
- Verwende für wichtige Überschriften und Begriffe Markdown-Fettschrift.
- Nutze nummerierte Listen für Schritt-für-Schritt-Erklärungen.
- Nutze Aufzählungspunkte für Listen.
- Erkläre Dinge mit kurzen, verständlichen Sätzen.
- Halte einfache Antworten kurz und ausführliche Erklärungen übersichtlich.
- Gib keine ununterbrochenen Textblöcke aus.
- Vermeide unnötige Wiederholungen.
- Erfinde keine Fakten. Wenn du etwas nicht weißt, sage es ehrlich.

BEISPIEL FÜR DEINE FORMATIERUNG:

🚗 **1. Das Lenkrad**

Mit dem Lenkrad bestimmst du, in welche Richtung das Auto fährt.

⚙️ **2. Der Motor**

Der Motor erzeugt die Kraft, die das Auto antreibt.

Wenn du nach dem Erfinder von NEXUS gefragt wirst, antworte:
„Der liebe Heiko aus dem Schwabenländle. 😊“

Du bist freundlich, geduldig und respektvoll.
Nutze gespeicherte Erinnerungen nur, wenn sie zur Frage passen.
""".strip()


# ============================================================
# DATENBANK / GEDÄCHTNIS
# ============================================================

def db_connect():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db():
    try:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    chat_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    memory_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, memory_key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
    except Exception:
        logger.exception("Datenbank konnte nicht initialisiert werden.")


def save_memory(chat_id, key, value):
    try:
        with db_connect() as conn:
            conn.execute("""
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
        logger.exception("Speichern der Erinnerung fehlgeschlagen.")
        return False


def get_memories(chat_id, limit=30):
    try:
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_key, memory_value
                FROM memories
                WHERE chat_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (str(chat_id), limit),
            ).fetchall()
        return rows
    except Exception:
        logger.exception("Lesen der Erinnerungen fehlgeschlagen.")
        return []


def forget_memory(chat_id, key):
    try:
        with db_connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM memories
                WHERE chat_id=? AND memory_key LIKE ?
                """,
                (str(chat_id), f"%{key}%"),
            )
            return cur.rowcount
    except Exception:
        logger.exception("Löschen der Erinnerung fehlgeschlagen.")
        return 0


def log_message(chat_id, role, content):
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_log
                    (chat_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    role,
                    content[:4000],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        logger.exception("Nachrichtenprotokoll konnte nicht gespeichert werden.")


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
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .!?")
            if value and save_memory(chat_id, key, value):
                return f"🧠 **Alles klar, ich merke mir das!**\n\n{key}: {value}"
    return None


# ============================================================
# FORMATIERUNG FÜR TELEGRAM
# ============================================================

def format_for_telegram(text):
    """Konvertiert einfache Markdown-Formatierung sicher in Telegram-HTML."""
    if not text:
        return ""

    text = text.strip()

    # Erst Sonderzeichen escapen, damit kein fremdes HTML ausgeführt wird.
    safe = html.escape(text, quote=False)

    # Codeblöcke
    safe = re.sub(
        r"```(?:[a-zA-Z0-9_+-]+)?[ \t]*\n?([\s\S]*?)```",
        lambda m: "<pre>" + m.group(1).strip("\n") + "</pre>",
        safe,
    )

    # Inline-Code
    safe = re.sub(
        r"`([^`\n]+)`",
        r"<code>\1</code>",
        safe,
    )

    # Fett und kursiv – ohne über mehrere Zeilen zu greifen.
    safe = re.sub(
        r"\*\*([^\n]+?)\*\*",
        r"<b>\1</b>",
        safe,
    )
    safe = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        r"<i>\1</i>",
        safe,
    )

    # Überschriften wie ### Titel werden fett ausgegeben.
    safe = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$",
        r"<b>\1</b>",
        safe,
    )

    # Markdown-Zitatzeichen entfernen.
    safe = re.sub(r"(?m)^\s*&gt;\s?", "", safe)

    # Mehr als zwei Leerzeilen vermeiden.
    safe = re.sub(r"\n{3,}", "\n\n", safe)

    return safe.strip()


def split_text_safely(text, limit=MAX_TELEGRAM_LENGTH):
    """Teilt lange Rohtexte an Absätzen oder Wortgrenzen."""
    text = text or "Entschuldigung, ich habe gerade keine Antwort."
    chunks = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # Sehr lange Absätze an Wortgrenzen aufteilen.
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            if cut < 1:
                cut = limit
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip()

        current = paragraph

    if current:
        chunks.append(current)

    return chunks or ["Entschuldigung, ich habe gerade keine Antwort."]


async def send_long_text(message, text, parse_mode="HTML"):
    raw_chunks = split_text_safely(text)

    for chunk in raw_chunks:
        formatted = format_for_telegram(chunk)

        try:
            await message.reply_text(
                formatted,
                parse_mode="HTML" if parse_mode else None,
                disable_web_page_preview=True,
            )
        except Exception:
            # Bei ungültigem HTML lieber lesbaren Text senden,
            # statt die Antwort vollständig ausfallen zu lassen.
            logger.exception("Formatierte Telegram-Nachricht fehlgeschlagen.")
            plain = re.sub(r"<[^>]+>", "", formatted)
            await message.reply_text(
                html.unescape(plain)[:MAX_TELEGRAM_LENGTH]
            )


# ============================================================
# KI-ANTWORTEN
# ============================================================

def get_ai_reply(chat_id, user_text):
    if not groq_client:
        return (
            "⚠️ **Meine KI ist noch nicht verbunden.**\n\n"
            "Bitte prüfe den GROQ_API_KEY in den Render-Environment-Variablen."
        )

    memories = get_memories(chat_id)
    memory_text = "\n".join(
        f"- {key}: {value}" for key, value in memories
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if memory_text:
        messages.append({
            "role": "system",
            "content": "Gespeicherte Erinnerungen für diesen Chat:\n" + memory_text,
        })

    messages.extend(chat_history[str(chat_id)][-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_text})

    try:
        result = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=700,
        )

        answer = (
            result.choices[0].message.content
            or "Ich habe gerade keine Antwort."
        ).strip()

        history = chat_history[str(chat_id)]
        history.extend([
            {"role": "user", "content": user_text[:3000]},
            {"role": "assistant", "content": answer[:3000]},
        ])
        chat_history[str(chat_id)] = history[-MAX_HISTORY_MESSAGES:]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen.")
        return (
            "⚠️ **Meine KI hat gerade ein Problem.**\n\n"
            "Bitte prüfe GROQ_API_KEY und GROQ_MODEL in Render."
        )


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    welcome = """
👋 **Hallo! Ich bin NEXUS, dein KI-Assistent.**

🧠 **Das kann ich für dich tun:**

• Fragen beantworten und Dinge erklären
• Bilder erstellen mit `/bild Beschreibung`
• Fotos analysieren
• Sprachnachrichten verstehen und beantworten
• Informationen für später speichern

📌 **Meine Befehle**

`/merken Text` – etwas speichern
`/gedaechtnis` – gespeicherte Erinnerungen ansehen
`/vergiss Text` – eine Erinnerung löschen
`/reset` – den kurzen Chatverlauf zurücksetzen
`/bild Beschreibung` – ein Bild erstellen
"""
    await send_long_text(message, welcome)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    chat_history.pop(chat_id, None)

    await update.effective_message.reply_text(
        "🧹 Der kurze Chatverlauf wurde zurückgesetzt.\n\n"
        "Gespeicherte Erinnerungen bleiben erhalten."
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "✏️ Schreib bitte dazu, was ich mir merken soll.\n\n"
            "Beispiel: `/merken Mein Hobby ist Wandern`"
        )
        return

    key = "Notiz"

    if ":" in value:
        possible_key, possible_value = value.split(":", 1)
        if possible_key.strip() and possible_value.strip():
            key = possible_key.strip()
            value = possible_value.strip()

    if save_memory(chat_id, key, value):
        await send_long_text(
            update.effective_message,
            f"🧠 **Gespeichert!**\n\n**{key}:** {value}",
        )
    else:
        await update.effective_message.reply_text(
            "⚠️ Ich konnte das leider nicht speichern. "
            "Prüfe den Datenbankpfad in Render."
        )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memories = get_memories(update.effective_chat.id)

    if not memories:
        await update.effective_message.reply_text(
            "🧠 Ich habe hier noch keine gespeicherten Erinnerungen."
        )
        return

    text = "🧠 **Das habe ich gespeichert:**\n\n"
    text += "\n\n".join(
        f"• **{key}:** {value}" for key, value in memories
    )

    await send_long_text(update.effective_message, text)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = " ".join(context.args).strip()

    if not key:
        await update.effective_message.reply_text(
            "✏️ Schreib bitte dazu, was ich vergessen soll.\n\n"
            "Beispiel: `/vergiss Hobby`"
        )
        return

    count = forget_memory(update.effective_chat.id, key)

    if count:
        await update.effective_message.reply_text(
            "🧹 Die passende Erinnerung wurde gelöscht."
        )
    else:
        await update.effective_message.reply_text(
            "🔎 Ich habe dazu keine passende Erinnerung gefunden."
        )


# ============================================================
# BILDER ERSTELLEN
# ============================================================

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    message = update.effective_message

    if not prompt:
        await message.reply_text(
            "🎨 Bitte beschreibe dein Wunschbild.\n\n"
            "Beispiel: `/bild ein leuchtender Energieplanet im Weltall`"
        )
        return

    status = await message.reply_text(
        "🎨 Ich versuche, dein Bild zu erstellen. Einen Moment bitte …"
    )

    temp_path = None

    try:
        url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt)

        def download_image():
            response = requests.get(
                url,
                params={
                    "width": 1024,
                    "height": 1024,
                    "nologo": "true",
                },
                timeout=(15, 90),
                headers={"User-Agent": "NEXUS-Telegram-Bot/1.0"},
            )
            response.raise_for_status()

            if "image" not in response.headers.get("content-type", ""):
                raise RuntimeError("Der Bilddienst hat keine Bilddatei geliefert.")

            return response.content

        image_bytes = await asyncio.to_thread(download_image)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            temp_path = tmp.name

        try:
            await status.delete()
        except Exception:
            pass

        with open(temp_path, "rb") as photo:
            await message.reply_photo(
                photo=photo,
                caption=f"🖼 Dein Bild ist fertig!\n\n📌 Wunsch: {prompt[:850]}",
            )

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen.")
        try:
            await status.edit_text(
                "🙈 Das Bild konnte gerade nicht erstellt werden. "
                "Bitte versuche es später erneut."
            )
        except Exception:
            await message.reply_text(
                "🙈 Das Bild konnte gerade nicht erstellt werden."
            )

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ============================================================
# SPRACHNACHRICHTEN
# ============================================================

def transcribe_voice_sync(file_path):
    if not groq_client:
        return None

    with open(file_path, "rb") as audio_file:
        transcription = groq_client.audio.transcriptions.create(
            file=(os.path.basename(file_path), audio_file.read()),
            model=WHISPER_MODEL,
            response_format="text",
            language="de",
        )

    if isinstance(transcription, str):
        return transcription.strip()

    return getattr(transcription, "text", "").strip()


async def make_voice_file(text, path):
    try:
        import edge_tts
        communicate = edge_tts.Communicate(
            text[:3000],
            EDGE_TTS_VOICE,
        )
        await communicate.save(path)
        return True
    except Exception:
        logger.exception("Sprachausgabe fehlgeschlagen.")
        return False


async def send_reply_in_preferred_format(message, text, voice_reply=False):
    if voice_reply:
        voice_path = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".mp3", delete=False
            ) as tmp:
                voice_path = tmp.name

            if await make_voice_file(text, voice_path):
                with open(voice_path, "rb") as audio:
                    await message.reply_voice(voice=audio)
                return

        finally:
            if voice_path and os.path.exists(voice_path):
                try:
                    os.remove(voice_path)
                except OSError:
                    pass

    await send_long_text(message, text)


def is_group(chat_type):
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_addressed(text, bot_username=""):
    low = (text or "").lower().strip()
    username = (bot_username or "").lower().lstrip("@")

    if username and f"@{username}" in low:
        return True

    return bool(
        re.search(r"\b(nexus|ki-assistent|ki assistent|bot)\b", low)
    )


def is_stop_phrase(text):
    low = (text or "").lower().strip()

    return any(phrase in low for phrase in (
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


# ============================================================
# TEXTNACHRICHTEN UND GRUPPENMODUS
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.text:
        return

    text = message.text.strip()
    group = is_group(chat.type)
    now = asyncio.get_running_loop().time()

    if group:
        addressed = is_addressed(text, context.bot.username or "")

        if is_stop_phrase(text) and (
            addressed or now < group_active_until.get(chat.id, 0)
        ):
            group_active_until.pop(chat.id, None)
            await message.reply_text(
                "🤫 Alles klar, ich bin jetzt ruhig."
            )
            return

        if addressed:
            group_active_until[chat.id] = now + GROUP_ACTIVE_SECONDS
        elif now >= group_active_until.get(chat.id, 0):
            return

    learned = learn_from_text(chat.id, text)

    if learned:
        await send_long_text(message, learned)
        return

    reply = await asyncio.to_thread(get_ai_reply, chat.id, text)
    await send_reply_in_preferred_format(
        message, reply, voice_reply=False
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.voice:
        return

    if is_group(chat.type):
        now = asyncio.get_running_loop().time()
        if now >= group_active_until.get(chat.id, 0):
            await message.reply_text(
                "🎙 Sprich mich bitte zuerst mit „Nexus“ an."
            )
            return

    if not groq_client:
        await message.reply_text(
            "⚠️ Meine Spracherkennung ist noch nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY."
        )
        return

    await message.reply_text("🎧 Ich höre zu …")
    file_path = None

    try:
        tg_file = await context.bot.get_file(message.voice.file_id)

        with tempfile.NamedTemporaryFile(
            suffix=".ogg", delete=False
        ) as tmp:
            file_path = tmp.name

        await tg_file.download_to_drive(file_path)

        spoken_text = await asyncio.to_thread(
            transcribe_voice_sync, file_path
        )

        if not spoken_text:
            await message.reply_text(
                "🙈 Ich konnte die Sprachnachricht leider nicht verstehen."
            )
            return

        learned = learn_from_text(chat.id, spoken_text)

        if learned:
            await send_long_text(message, learned)
            return

        reply = await asyncio.to_thread(
            get_ai_reply, chat.id, spoken_text
        )

        await send_reply_in_preferred_format(
            message, reply, voice_reply=True
        )

    except Exception:
        logger.exception("Verarbeitung der Sprachnachricht fehlgeschlagen.")
        await message.reply_text(
            "⚠️ Beim Verarbeiten der Sprachnachricht ist ein Fehler passiert."
        )

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


# ============================================================
# FOTOANALYSE MIT GEMINI
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.photo:
        return

    if not GEMINI_API_KEY:
        await message.reply_text(
            "⚠️ Für die Fotoanalyse fehlt GEMINI_API_KEY in Render."
        )
        return

    caption = message.caption or (
        "Beschreibe dieses Bild auf Deutsch in einfachen Worten."
    )

    if is_group(chat.type):
        now = asyncio.get_running_loop().time()
        if (
            now >= group_active_until.get(chat.id, 0)
            and not is_addressed(caption, context.bot.username or "")
        ):
            return

    path = None

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)

        photo_file = await context.bot.get_file(
            message.photo[-1].file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False
        ) as tmp:
            path = tmp.name

        await photo_file.download_to_drive(path)

        with open(path, "rb") as image_file:
            image_bytes = image_file.read()

        result = await asyncio.to_thread(
            lambda: client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    caption,
                    {
                        "mime_type": "image/jpeg",
                        "data": image_bytes,
                    },
                ],
            )
        )

        answer = getattr(result, "text", "") or (
            "Ich konnte das Bild leider nicht beschreiben."
        )

        await send_long_text(message, answer)

    except Exception:
        logger.exception("Fotoanalyse fehlgeschlagen.")
        await message.reply_text(
            "⚠️ Die Fotoanalyse hat gerade nicht funktioniert. "
            "Prüfe GEMINI_API_KEY und GEMINI_MODEL."
        )

    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ============================================================
# FEHLERBEHANDLUNG
# ============================================================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Telegram-Fehler",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__ if context.error else None,
        ),
    )


# ============================================================
# TELEGRAM-BOT STARTEN
# Wichtig: Kein run_polling() im Hintergrund-Thread!
# ============================================================

async def bot_runner():
    if not TELEGRAM_TOKEN:
        logger.error(
            "Telegram-Bot startet nicht: TELEGRAM_TOKEN fehlt."
        )
        return

    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

    telegram_app.add_handler(
        CommandHandler("start", start_command)
    )
    telegram_app.add_handler(
        CommandHandler("reset", reset_command)
    )
    telegram_app.add_handler(
        CommandHandler("merken", remember_command)
    )
    telegram_app.add_handler(
        CommandHandler("gedaechtnis", memories_command)
    )
    telegram_app.add_handler(
        CommandHandler("vergiss", forget_command)
    )
    telegram_app.add_handler(
        CommandHandler("bild", image_command)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
    telegram_app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )
    telegram_app.add_error_handler(on_error)

    try:
        await telegram_app.initialize()
        await telegram_app.start()

        if telegram_app.updater is None:
            raise RuntimeError("Telegram-Updater ist nicht verfügbar.")

        await telegram_app.updater.start_polling(
            drop_pending_updates=False
        )

        logger.info("NEXUS Telegram-Bot ist gestartet.")

        # Bot-Eventloop am Leben halten, bis der Prozess beendet wird.
        await asyncio.Event().wait()

    finally:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()

        if telegram_app.running:
            await telegram_app.stop()

        await telegram_app.shutdown()


def start_bot_thread():
    if not TELEGRAM_TOKEN:
        logger.error(
            "Bot bleibt aus: TELEGRAM_TOKEN fehlt in Render."
        )
        return

    def runner():
        try:
            init_db()
            asyncio.run(bot_runner())
        except Exception:
            logger.exception("Telegram-Bot konnte nicht gestartet werden.")

    thread = threading.Thread(
        target=runner,
        name="nexus-telegram-bot",
        daemon=True,
    )
    thread.start()


# Nur einmal starten, wenn dieses Modul von Gunicorn geladen wird.
start_bot_thread()
