
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
# NEXUS KI-ASSISTENT — Render + Telegram
#
# Benötigte Render-Variablen:
# TELEGRAM_TOKEN
# GROQ_API_KEY
#
# Optionale Variablen:
# GROQ_MODEL
# WHISPER_MODEL
# EDGE_TTS_VOICE
# GEMINI_API_KEY
# GEMINI_MODEL
# DATABASE_PATH
# ============================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("nexus")

# Flask-Webserver für Render
app = Flask(__name__)


@app.get("/")
def home():
    return "NEXUS KI-Assistent läuft! 🤖", 200


@app.get("/health")
def health():
    return "OK", 200


# ============================================================
# EINSTELLUNGEN
# Bestehende Render-Umgebungsvariablen bleiben erhalten.
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
).strip()

WHISPER_MODEL = os.getenv(
    "WHISPER_MODEL",
    "whisper-large-v3",
).strip()

EDGE_TTS_VOICE = os.getenv(
    "EDGE_TTS_VOICE",
    "de-DE-ConradNeural",
).strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash",
).strip()

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/nexus_memory.sqlite3"
    if Path("/var/data").exists()
    else "nexus_memory.sqlite3",
)

if not TELEGRAM_TOKEN:
    logger.warning(
        "TELEGRAM_TOKEN fehlt. Telegram-Bot wird nicht gestartet."
    )

if not GROQ_API_KEY:
    logger.warning(
        "GROQ_API_KEY fehlt. KI-Antworten funktionieren nicht."
    )

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ============================================================
# KI-PROMPT
# ============================================================

SYSTEM_PROMPT = """
Du bist NEXUS, ein freundlicher deutscher KI-Assistent.

Antworte auf Deutsch, kurz, klar und leicht verständlich.
Nutze bei Bedarf wenige passende Emojis und eine moderne,
übersichtliche Formatierung.

WICHTIG FÜR DIE FORMATIERUNG:
- Verwende klare, kurze Überschriften.
- Setze zwischen Überschrift und Erklärung eine Leerzeile.
- Trenne unterschiedliche Themen durch Leerzeilen.
- Verwende Aufzählungen, wenn sie das Verständnis verbessern.
- Verwende keine riesigen Textblöcke.
- Verwende Telegram-kompatible Markdown-Formatierung.
- Setze Fettformatierung sparsam und korrekt ein.
- Erfinde keine Fakten.
- Wenn du etwas nicht weißt, sage es ehrlich.

Wenn jemand fragt, wer dich programmiert oder erfunden hat,
antworte:
"Der liebe Heiko aus dem Schwabenländle. 😊"

Du bist freundlich, geduldig und respektvoll.
""".strip()


# ============================================================
# CHATVERLAUF UND GRUPPENSTATUS
# ============================================================

chat_history = defaultdict(list)
group_active_until = {}

MAX_HISTORY_MESSAGES = 12
GROUP_ACTIVE_SECONDS = 180


# ============================================================
# DATENBANK
# ============================================================

def db_connect():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        str(path),
        timeout=30,
    )
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
        logger.exception(
            "Datenbank konnte nicht initialisiert werden."
        )


def save_memory(chat_id, key, value):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO memories (
                    chat_id,
                    memory_key,
                    memory_value,
                    updated_at
                )
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
        logger.exception(
            "Speichern der Erinnerung fehlgeschlagen."
        )
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
        logger.exception(
            "Lesen der Erinnerungen fehlgeschlagen."
        )
        return []


def forget_memory(chat_id, key):
    try:
        with db_connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM memories
                WHERE chat_id=?
                AND memory_key LIKE ?
                """,
                (str(chat_id), f"%{key}%"),
            )

            return cur.rowcount

    except Exception:
        logger.exception(
            "Löschen der Erinnerung fehlgeschlagen."
        )
        return 0


def log_message(chat_id, role, content):
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_log (
                    chat_id,
                    role,
                    content,
                    created_at
                )
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
        logger.exception(
            "Nachrichtenprotokoll konnte nicht gespeichert werden."
        )


# ============================================================
# ERINNERUNGEN AUTOMATISCH ERKENNEN
# ============================================================

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
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            value = match.group(1).strip(" .!?")

            if value:
                save_memory(chat_id, key, value)

                return (
                    f"Alles klar, ich merke mir: "
                    f"{key} – {value} 😊"
                )

    return None


def escape_html(text):
    return html.escape(text or "", quote=False)


# ============================================================
# NACHRICHTEN SENDEN
# ============================================================

async def send_long_text(message, text, parse_mode=None):
    text = text or "Entschuldigung, ich habe gerade keine Antwort."

    # Telegram erlaubt ungefähr 4096 Zeichen je Nachricht.
    # Bei normalem Text bleiben wir unter dieser Grenze.
    for start in range(0, len(text), 4000):
        await message.reply_text(
            text[start:start + 4000],
            parse_mode=parse_mode,
        )


# ============================================================
# GROQ-KI-ANTWORT
# ============================================================

def get_ai_reply(chat_id, user_text):
    if not groq_client:
        return (
            "Meine KI ist noch nicht verbunden. "
            "Bitte prüfe den GROQ_API_KEY in Render."
        )

    memories = get_memories(chat_id)

    memory_text = "\n".join(
        f"- {key}: {value}"
        for key, value in memories
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    if memory_text:
        messages.append({
            "role": "system",
            "content": (
                "Gespeicherte Erinnerungen für diesen Chat:\n"
                + memory_text
            ),
        })

    messages.extend(
        chat_history[str(chat_id)][-MAX_HISTORY_MESSAGES:]
    )

    messages.append({
        "role": "user",
        "content": user_text,
    })

    try:
        result = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=400,
        )

        answer = (
            result.choices[0].message.content
            or "Ich habe gerade keine Antwort."
        )

        history = chat_history[str(chat_id)]

        history.extend([
            {
                "role": "user",
                "content": user_text[:3000],
            },
            {
                "role": "assistant",
                "content": answer[:3000],
            },
        ])

        chat_history[str(chat_id)] = history[
            -MAX_HISTORY_MESSAGES:
        ]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen.")

        return (
            "Ups, meine KI hat gerade ein Problem. "
            "Prüfe bitte den GROQ_API_KEY und GROQ_MODEL in Render."
        )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "Hallo! Ich bin NEXUS, dein KI-Assistent. 🤖\n\n"
        "Schreib mir einfach eine Nachricht.\n\n"
        "Befehle:\n"
        "/merken Text – etwas speichern\n"
        "/gedaechtnis – Erinnerungen ansehen\n"
        "/vergiss Text – Erinnerung löschen\n"
        "/reset – Chat-Verlauf zurücksetzen\n"
        "/bild Beschreibung – Bild erstellen"
    )


# ============================================================
# /RESET
# ============================================================

async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = str(update.effective_chat.id)

    chat_history.pop(chat_id, None)

    await update.effective_message.reply_text(
        "Der kurze Chat-Verlauf wurde zurückgesetzt. 🧹\n"
        "Gespeicherte Erinnerungen bleiben erhalten."
    )


# ============================================================
# /MERKEN
# ============================================================

async def remember_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Schreib bitte dazu, was ich mir merken soll.\n"
            "Beispiel: /merken Mein Hobby ist Wandern"
        )
        return

    key = "Notiz"

    if ":" in value:
        possible_key, possible_value = value.split(":", 1)

        if possible_key.strip() and possible_value.strip():
            key = possible_key.strip()
            value = possible_value.strip()

    if save_memory(chat_id, key, value):
        await update.effective_message.reply_text(
            f"Gespeichert! 🧠\n{key}: {value}"
        )
    else:
        await update.effective_message.reply_text(
            "Ich konnte das leider nicht speichern. "
            "Prüfe den Datenbankpfad in Render."
        )


# ============================================================
# /GEDAECHTNIS
# ============================================================

async def memories_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    memories = get_memories(update.effective_chat.id)

    if not memories:
        await update.effective_message.reply_text(
            "Ich habe hier noch keine gespeicherten Erinnerungen. 🧠"
        )
        return

    text = (
        "🧠 Das habe ich gespeichert:\n\n"
        + "\n".join(
            f"• {key}: {value}"
            for key, value in memories
        )
    )

    await send_long_text(
        update.effective_message,
        text,
    )


# ============================================================
# /VERGISS
# ============================================================

async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    key = " ".join(context.args).strip()

    if not key:
        await update.effective_message.reply_text(
            "Schreib bitte dazu, was ich vergessen soll.\n"
            "Beispiel: /vergiss Hobby"
        )
        return

    count = forget_memory(
        update.effective_chat.id,
        key,
    )

    if count:
        await update.effective_message.reply_text(
            "Erinnerung gelöscht. 🧹"
        )
    else:
        await update.effective_message.reply_text(
            "Ich habe dazu keine passende Erinnerung gefunden."
        )


# ============================================================
# /BILD — BILDERSTELLUNG
# ============================================================

async def image_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    prompt = " ".join(context.args).strip()

    if not prompt:
        await update.effective_message.reply_text(
            "Bitte schreibe eine Bildbeschreibung dazu.\n"
            "Beispiel: /bild ein leuchtender Energieplanet im Weltall"
        )
        return

    await update.effective_message.reply_text(
        "Ich versuche, dein Bild zu erstellen. 🎨"
    )

    temp_path = None

    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            + requests.utils.quote(prompt)
        )

        # Abruf erfolgt außerhalb des Telegram-Event-Loops.
        def download_image():
            response = requests.get(
                url,
                params={
                    "width": 1024,
                    "height": 1024,
                    "nologo": "true",
                },
                timeout=90,
            )
            response.raise_for_status()

            content_type = response.headers.get(
                "content-type",
                "",
            ).lower()

            if not content_type.startswith("image/"):
                raise RuntimeError(
                    "Der Bilddienst hat kein echtes Bild geliefert. "
                    f"Content-Type: {content_type}"
                )

            if not response.content:
                raise RuntimeError(
                    "Der Bilddienst hat eine leere Datei geliefert."
                )

            return response.content

        image_bytes = await asyncio.to_thread(download_image)

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as tmp:
            tmp.write(image_bytes)
            temp_path = tmp.name

        with open(temp_path, "rb") as photo:
            await update.effective_message.reply_photo(
                photo=photo,
                caption=prompt[:900],
            )

    except Exception as exc:
        logger.exception(
            "Bildgenerierung fehlgeschlagen (%s): %s",
            type(exc).__name__,
            str(exc),
        )

        await update.effective_message.reply_text(
            "⚠️ Das Bild konnte gerade nicht erstellt werden. "
            "Der genaue Fehler steht in den Render-Logs."
        )

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning(
                    "Temporäre Bilddatei konnte nicht gelöscht werden."
                )


# ============================================================
# SPRACHERKENNUNG — GROQ WHISPER
# ============================================================

async def transcribe_voice(file_path):
    if not groq_client:
        return None

    with open(file_path, "rb") as audio_file:
        transcription = groq_client.audio.transcriptions.create(
            file=(
                os.path.basename(file_path),
                audio_file.read(),
            ),
            model=WHISPER_MODEL,
            response_format="text",
            language="de",
        )

    if isinstance(transcription, str):
        return transcription.strip()

    return getattr(
        transcription,
        "text",
        "",
    ).strip()


# ============================================================
# SPRACHAUSGABE — EDGE TTS
# ============================================================

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


async def send_reply_in_preferred_format(
    message,
    text,
    voice_reply=False,
):
    if voice_reply:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False,
        ) as tmp:
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

    await send_long_text(message, text)


# ============================================================
# GRUPPENFUNKTIONEN
# ============================================================

def is_group(chat_type):
    return chat_type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    )


def is_addressed(text, bot_username=""):
    low = (text or "").lower().strip()
    username = bot_username.lower().lstrip("@")

    if username and f"@{username}" in low:
        return True

    return bool(
        re.search(
            r"\b(nexus|ki-assistent|ki assistent|bot)\b",
            low,
        )
    )


def is_stop_phrase(text):
    low = (text or "").lower().strip()

    return any(
        phrase in low
        for phrase in (
            "nexus aus",
            "nexus stopp",
            "stopp nexus",
            "ruhe nexus",
            "tschüss nexus",
            "auf wiedersehen nexus",
            "geh offline",
            "sei still nexus",
            "nexus schlaf",
        )
    )


# ============================================================
# TEXTNACHRICHTEN VERARBEITEN
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.text:
        return

    text = message.text.strip()
    group = is_group(chat.type)
    now = asyncio.get_running_loop().time()

    if group:
        addressed = is_addressed(
            text,
            context.bot.username or "",
        )

        if (
            is_stop_phrase(text)
            and (
                addressed
                or now < group_active_until.get(chat.id, 0)
            )
        ):
            group_active_until.pop(chat.id, None)

            await message.reply_text(
                "Alles klar, ich bin jetzt ruhig. 👋"
            )
            return

        if addressed:
            group_active_until[chat.id] = (
                now + GROUP_ACTIVE_SECONDS
            )
        elif now >= group_active_until.get(chat.id, 0):
            return

    learned = learn_from_text(chat.id, text)

    if learned:
        await message.reply_text(learned)
        return

    reply = await asyncio.to_thread(
        get_ai_reply,
        chat.id,
        text,
    )

    await send_reply_in_preferred_format(
        message,
        reply,
        voice_reply=False,
    )


# ============================================================
# SPRACHNACHRICHTEN VERARBEITEN
# ============================================================

async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.voice:
        return

    group = is_group(chat.type)
    now = asyncio.get_running_loop().time()

    if (
        group
        and now >= group_active_until.get(chat.id, 0)
    ):
        await message.reply_text(
            "Sprich mich bitte zuerst mit „Nexus“ an. 😊"
        )
        return

    if not groq_client:
        await message.reply_text(
            "Meine Spracherkennung ist noch nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY."
        )
        return

    await message.reply_text("Ich höre zu … 🎧")

    file_path = None

    try:
        tg_file = await context.bot.get_file(
            message.voice.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False,
        ) as tmp:
            file_path = tmp.name

        await tg_file.download_to_drive(file_path)

        spoken_text = await asyncio.to_thread(
            lambda: asyncio.run(
                transcribe_voice(file_path)
            )
        )

        if not spoken_text:
            await message.reply_text(
                "Ich konnte die Sprachnachricht leider nicht verstehen."
            )
            return

        learned = learn_from_text(
            chat.id,
            spoken_text,
        )

        if learned:
            await message.reply_text(learned)
            return

        reply = await asyncio.to_thread(
            get_ai_reply,
            chat.id,
            spoken_text,
        )

        await send_reply_in_preferred_format(
            message,
            reply,
            voice_reply=True,
        )

    except Exception:
        logger.exception(
            "Verarbeitung der Sprachnachricht fehlgeschlagen."
        )

        await message.reply_text(
            "Beim Verarbeiten der Sprachnachricht ist ein Fehler passiert."
        )

    finally:
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass


# ============================================================
# FOTOANALYSE — GEMINI
# ============================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.photo or not chat:
        return

    if not GEMINI_API_KEY:
        await message.reply_text(
            "Für Fotoanalyse fehlt noch GEMINI_API_KEY in Render."
        )
        return

    caption = (
        message.caption
        or "Beschreibe dieses Bild auf Deutsch in einfachen Worten."
    )

    # Gruppenverhalten beibehalten
    if is_group(chat.type):
        now = asyncio.get_running_loop().time()

        if (
            now >= group_active_until.get(chat.id, 0)
            and not is_addressed(
                caption,
                context.bot.username or "",
            )
        ):
            return

    path = None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        # Foto aus Telegram laden
        photo_file = await context.bot.get_file(
            message.photo[-1].file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as tmp:
            path = tmp.name

        await photo_file.download_to_drive(path)

        with open(path, "rb") as image_file:
            image_bytes = image_file.read()

        if not image_bytes:
            raise ValueError(
                "Die heruntergeladene Bilddatei ist leer."
            )

        # Gemini erhält den Text und die Bilddaten
        def analyze_photo():
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    caption,
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type="image/jpeg",
                    ),
                ],
            )

        result = await asyncio.to_thread(
            analyze_photo
        )

        answer = getattr(
            result,
            "text",
            None,
        )

        if not answer or not answer.strip():
            logger.error(
                "Gemini hat keinen lesbaren Antworttext geliefert."
            )

            await message.reply_text(
                "Ich konnte das Bild nicht beschreiben. "
                "Bitte versuche es mit einem anderen Foto erneut."
            )
            return

        await send_long_text(
            message,
            answer.strip(),
        )

    except Exception as exc:
        # Konkrete Fehler protokollieren.
        # API-Schlüssel nicht in die Fehlermeldung aufnehmen.
        logger.exception(
            "Fotoanalyse fehlgeschlagen (%s): %s",
            type(exc).__name__,
            str(exc),
        )

        await message.reply_text(
            "⚠️ Die Fotoanalyse ist fehlgeschlagen. "
            "Der genaue Fehler steht jetzt in den Render-Logs."
        )

    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                logger.warning(
                    "Temporäre Fotodatei konnte nicht gelöscht werden."
                )


# ============================================================
# FEHLERBEHANDLUNG
# ============================================================

async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram-Fehler: %s",
        context.error,
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ) if context.error else None,
    )


# ============================================================
# TELEGRAM-BOT STARTEN
# ============================================================

def main():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error(
            "Bot startet nicht: TELEGRAM_TOKEN fehlt."
        )
        return

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("reset", reset_command)
    )

    application.add_handler(
        CommandHandler("merken", remember_command)
    )

    application.add_handler(
        CommandHandler("gedaechtnis", memories_command)
    )

    application.add_handler(
        CommandHandler("vergiss", forget_command)
    )

    application.add_handler(
        CommandHandler("bild", image_command)
    )

    application.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    application.add_error_handler(on_error)

    logger.info("NEXUS Telegram-Bot startet.")

    # Polling bleibt wie bisher eingerichtet.
    application.run_polling(
        drop_pending_updates=False
    )


# ============================================================
# HINTERGRUNDTHREAD FÜR RENDER / GUNICORN
# ============================================================

_bot_thread = None


def start_bot_thread():
    global _bot_thread

    if _bot_thread and _bot_thread.is_alive():
        return

    if not TELEGRAM_TOKEN:
        logger.warning(
            "Telegram-Bot bleibt aus, da TELEGRAM_TOKEN fehlt."
        )
        return

    def runner():
        try:
            main()
        except Exception:
            logger.exception(
                "Telegram-Bot konnte nicht gestartet werden."
            )

    _bot_thread = threading.Thread(
        target=runner,
        name="nexus-telegram-bot",
        daemon=True,
    )

    _bot_thread.start()


# Bot beim Import durch Gunicorn starten
start_bot_thread()
