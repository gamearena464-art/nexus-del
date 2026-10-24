
import os
import io
import re
import html
import time
import asyncio
import logging
import sqlite3
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify
from PIL import Image
from groq import Groq
from google import genai
from google.genai import types

from telegram import Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# EINSTELLUNGEN
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "openai/gpt-oss-120b"
).strip()

WHISPER_MODEL = os.getenv(
    "WHISPER_MODEL", "whisper-large-v3"
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()

EDGE_TTS_VOICE = os.getenv(
    "EDGE_TTS_VOICE", "de-DE-ConradNeural"
).strip()

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/nexus_memory.sqlite3"
    if Path("/var/data").exists()
    else "nexus_memory.sqlite3",
)

MAX_MESSAGE_LENGTH = 3900
GROUP_ACTIVE_SECONDS = 180
MAX_HISTORY_MESSAGES = 12

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("NEXUS")
app = Flask(__name__)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

bot_application = None
bot_thread = None
bot_start_lock = threading.Lock()

group_active_until = {}
group_active_lock = threading.Lock()


# =========================================================
# WEB-SERVER FÜR RENDER
# =========================================================

@app.route("/")
def home():
    return jsonify({
        "name": "NEXUS KI-Assistent",
        "status": "online",
        "telegram_configured": bool(TELEGRAM_TOKEN),
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200


# =========================================================
# TELEGRAM-FORMATIERUNG
# =========================================================

def markdown_to_telegram_html(text):
    """
    Wandelt einfache Markdown-Formatierung in sicheres
    Telegram-HTML um. Absätze und Leerzeilen bleiben erhalten.
    """

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # HTML zuerst escapen, damit KI-Ausgaben keine HTML-Tags
    # einschleusen oder die Telegram-Nachricht beschädigen.
    text = html.escape(text, quote=False)

    # Codeblöcke schützen
    code_blocks = []

    def save_code_block(match):
        code = match.group(1)
        placeholder = f"NXCODEBLOCK{len(code_blocks)}END"
        code_blocks.append("<pre>" + code + "</pre>")
        return placeholder

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+-]+)?\s*\n?(.*?)```",
        save_code_block,
        text,
        flags=re.DOTALL,
    )

    # Inline-Code schützen
    inline_codes = []

    def save_inline_code(match):
        code = match.group(1)
        placeholder = f"NXINLINECODE{len(inline_codes)}END"
        inline_codes.append("<code>" + code + "</code>")
        return placeholder

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # Überschriften: # Titel -> fett
    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$",
        r"<b>\1</b>",
        text,
    )

    # Markdown-Fettschrift
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(
        r"__(.+?)__",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    # Markdown-Kursivschrift
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    # Aufzählungen vereinheitlichen
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)

    # Markdown-Links lesbar darstellen
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # Zu viele Leerzeilen vermeiden, aber Absätze behalten
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Platzhalter für Code wieder einsetzen
    for index, code in enumerate(code_blocks):
        text = text.replace(f"NXCODEBLOCK{index}END", code)

    for index, code in enumerate(inline_codes):
        text = text.replace(f"NXINLINECODE{index}END", code)

    return text.strip()


def plain_text_for_voice(text):
    """Entfernt Markdown-Zeichen für die Sprachausgabe."""

    if not text:
        return ""

    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)

    return text.strip()


# =========================================================
# SQLITE LANGZEITGEDÄCHTNIS
# =========================================================

def db_connect():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        str(path),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_db():
    with db_connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_chat
            ON memories(chat_id, id)
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, fact)
            )
        """)


def save_message(chat_id, user_id, role, content):
    if not content:
        return

    try:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO memories
                (chat_id, user_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    str(user_id),
                    role,
                    content[:12000],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            connection.execute(
                """
                DELETE FROM memories
                WHERE chat_id = ?
                AND id NOT IN (
                    SELECT id FROM memories
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT 500
                )
                """,
                (str(chat_id), str(chat_id)),
            )

    except Exception:
        logger.exception("Nachricht konnte nicht gespeichert werden")


def get_history(chat_id, limit=MAX_HISTORY_MESSAGES):
    try:
        with db_connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content
                FROM memories
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(chat_id), limit),
            ).fetchall()

        return [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(rows)
            if row["role"] in ("user", "assistant")
        ]

    except Exception:
        logger.exception("Chatverlauf konnte nicht gelesen werden")
        return []


def save_fact(user_id, fact):
    fact = fact.strip()

    if not fact or len(fact) > 500:
        return

    try:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO facts
                (user_id, fact, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    str(user_id),
                    fact,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    except Exception:
        logger.exception("Fakt konnte nicht gespeichert werden")


def get_facts(user_id):
    try:
        with db_connect() as connection:
            rows = connection.execute(
                """
                SELECT fact FROM facts
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 30
                """,
                (str(user_id),),
            ).fetchall()

        return [row["fact"] for row in rows]

    except Exception:
        logger.exception("Fakten konnten nicht gelesen werden")
        return []


def clear_chat_memory(chat_id):
    with db_connect() as connection:
        connection.execute(
            "DELETE FROM memories WHERE chat_id = ?",
            (str(chat_id),),
        )


# =========================================================
# KI-ANTWORTEN
# =========================================================

SYSTEM_PROMPT = """
Du bist NEXUS, ein freundlicher KI-Assistent.

Sprich Deutsch, außer der Nutzer wünscht eine andere Sprache.
Antworte einfach, freundlich und verständlich.

WICHTIG: Formatiere jede Antwort übersichtlich mit Markdown.

FORMATIERUNGSREGELN:
- Beginne längere Erklärungen mit einer passenden Überschrift.
- Schreibe Überschriften in einer eigenen Zeile.
- Nutze für wichtige Überschriften die Form **Überschrift**.
- Lasse nach Überschriften eine Leerzeile.
- Erkläre jeden Begriff in einem eigenen Absatz.
- Lasse zwischen zwei Abschnitten eine Leerzeile.
- Bei Erklärungen verwende nummerierte Abschnitte, wenn es sinnvoll ist.
- Beispiel:
  **1. Das Lenkrad**

  Mit dem Lenkrad bestimmst du die Richtung des Autos.

  **2. Der Motor**

  Der Motor erzeugt die Kraft, die das Auto antreibt.
- Vermeide lange Textblöcke ohne Absätze.
- Verwende Aufzählungen für Listen.
- Schreibe kurze, klare Sätze.
- Übertreibe es nicht mit Emojis.
- Behaupte nicht, etwas getan zu haben, wenn du es nicht getan hast.
- Erfinde keine persönlichen Erinnerungen.
- Wenn du nach deinem Erfinder oder Programmierer gefragt wirst,
  antworte genau:
  "Der liebe Heiko aus dem Schwabenländle. 😊"

Du bist eine KI und darfst dich nicht als Mensch ausgeben.
""".strip()


def groq_answer(messages):
    if not groq_client:
        return None

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=1000,
    )

    return (response.choices[0].message.content or "").strip()


def gemini_answer(prompt):
    if not gemini_client:
        return None

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    return (response.text or "").strip()


def ask_ai(chat_id, user_id, user_text):
    history = get_history(chat_id)
    facts = get_facts(user_id)

    fact_text = "\n".join(f"- {fact}" for fact in facts)

    if not fact_text:
        fact_text = "Noch keine gespeicherten Fakten."

    messages = [
        {
            "role": "system",
            "content": (
                SYSTEM_PROMPT
                + "\n\nGespeicherte Informationen über den Nutzer:\n"
                + fact_text
            ),
        }
    ]

    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        answer = groq_answer(messages)

        if answer:
            return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen")

    try:
        prompt = SYSTEM_PROMPT + "\n\n"
        prompt += "Gespeicherte Informationen:\n" + fact_text + "\n\n"
        prompt += "Bisheriger Verlauf:\n"

        for item in history:
            prompt += item["role"] + ": " + item["content"] + "\n"

        prompt += "\nNutzer: " + user_text

        answer = gemini_answer(prompt)

        if answer:
            return answer

    except Exception:
        logger.exception("Gemini-Anfrage fehlgeschlagen")

    return (
        "Entschuldigung 😊 Meine KI ist gerade nicht erreichbar. "
        "Bitte versuche es gleich noch einmal."
    )


# =========================================================
# GRUPPENMODUS
# =========================================================

def is_group(chat_type):
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def activate_group(chat_id):
    with group_active_lock:
        group_active_until[str(chat_id)] = (
            time.time() + GROUP_ACTIVE_SECONDS
        )


def deactivate_group(chat_id):
    with group_active_lock:
        group_active_until.pop(str(chat_id), None)


def group_is_active(chat_id):
    with group_active_lock:
        return group_active_until.get(str(chat_id), 0) > time.time()


def addressed_to_bot(text, bot_username=""):
    lowered = text.lower()

    triggers = (
        "nexus",
        "bot",
        "ki-assistent",
        "ki assistent",
    )

    if any(trigger in lowered for trigger in triggers):
        return True

    if bot_username and ("@" + bot_username.lower()) in lowered:
        return True

    return False


def stop_requested(text):
    text = text.lower().strip()

    stop_words = (
        "nexus stop",
        "nexus sei still",
        "nexus ruhe",
        "nexus tschüss",
        "nexus auf wiedersehen",
        "bot stop",
        "bot sei still",
        "bot ruhe",
    )

    return any(word in text for word in stop_words)


# =========================================================
# TELEGRAM-HILFSFUNKTIONEN
# =========================================================

async def send_long_message(message, text):
    text = text or "Ich habe gerade keine Antwort. 😊"

    # Formatierung in Telegram-HTML umwandeln.
    formatted = markdown_to_telegram_html(text)

    # Nachrichten sicher aufteilen, ohne zu lange Telegram-Nachrichten.
    chunks = []

    while len(formatted) > MAX_MESSAGE_LENGTH:
        split_at = formatted.rfind("\n", 0, MAX_MESSAGE_LENGTH)

        if split_at < 1000:
            split_at = MAX_MESSAGE_LENGTH

        chunks.append(formatted[:split_at])
        formatted = formatted[split_at:].lstrip("\n")

    if formatted:
        chunks.append(formatted)

    for chunk in chunks:
        try:
            await message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception(
                "Formatierte Nachricht konnte nicht gesendet werden"
            )

            # Fallback: Text ohne Formatierung senden.
            await message.reply_text(
                html.unescape(re.sub(r"<[^>]+>", "", chunk))
            )


async def get_bot_username(context):
    try:
        bot = await context.bot.get_me()
        return bot.username or ""
    except Exception:
        return ""


async def send_voice_reply(message, text):
    try:
        import edge_tts

        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "nexus.mp3")

            voice_text = plain_text_for_voice(text)

            communicate = edge_tts.Communicate(
                voice_text[:3000],
                EDGE_TTS_VOICE,
            )

            await communicate.save(output_path)

            with open(output_path, "rb") as audio:
                await message.reply_voice(voice=audio)

    except Exception:
        logger.exception("Sprachausgabe fehlgeschlagen")
        await send_long_message(message, text)


# =========================================================
# START / HILFE / GEDÄCHTNIS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    text = (
        "Hallo! 👋 Ich bin NEXUS, dein KI-Assistent.\n\n"
        "Du kannst mir schreiben oder eine Sprachnachricht senden.\n\n"
        "Befehle:\n"
        "/start – Starten\n"
        "/hilfe – Hilfe anzeigen\n"
        "/bild Beschreibung – Bild erstellen\n"
        "/gedaechtnis – Gespeicherte Fakten anzeigen\n"
        "/vergiss – Chatverlauf löschen\n\n"
        "In Gruppen sprich mich mit „Nexus“ an. 😊"
    )

    await send_long_message(update.effective_message, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    facts = get_facts(user.id)

    if not facts:
        await message.reply_text(
            "🧠 Ich habe noch keine besonderen Fakten gespeichert."
        )
        return

    result = "🧠 Das habe ich mir gemerkt:\n\n"
    result += "\n".join("• " + fact for fact in facts)

    await send_long_message(message, result)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    clear_chat_memory(chat.id)

    await message.reply_text(
        "🧹 Der gespeicherte Chatverlauf wurde gelöscht. "
        "Gesondert gespeicherte Fakten bleiben erhalten."
    )


# =========================================================
# BILDER ERSTELLEN
# =========================================================

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    prompt = " ".join(context.args).strip()

    if not prompt:
        await message.reply_text(
            "🎨 Schreibe bitte dazu, was ich malen soll.\n"
            "Beispiel: /bild ein leuchtender Energieplanet"
        )
        return

    if not gemini_client:
        await message.reply_text(
            "🎨 Die Bildfunktion ist noch nicht eingerichtet. "
            "Bitte hinterlege GEMINI_API_KEY bei Render."
        )
        return

    status = await message.reply_text(
        "🎨 Ich versuche, dein Bild zu erstellen …"
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Erzeuge ein Bild nach dieser Beschreibung: " + prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"]
            ),
        )

        image_bytes = None
        description = ""

        for candidate in (response.candidates or []):
            content = getattr(candidate, "content", None)

            for part in getattr(content, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)

                if inline_data and inline_data.data:
                    data = inline_data.data
                    image_bytes = (
                        data if isinstance(data, bytes)
                        else bytes(data)
                    )

                elif getattr(part, "text", None):
                    description += part.text

        if not image_bytes:
            await status.edit_text(
                "Leider konnte ich diesmal kein Bild erstellen. "
                "Prüfe bitte, ob dein Gemini-Modell Bildausgabe unterstützt."
            )

            if description:
                await send_long_message(message, description)

            return

        image = Image.open(io.BytesIO(image_bytes))
        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)

        await message.reply_photo(
            photo=output,
            caption=("🎨 " + prompt)[:1000],
        )

        await status.delete()

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen")

        await status.edit_text(
            "Beim Erstellen des Bildes ist ein Fehler aufgetreten. "
            "Prüfe den Gemini-Zugang und das verwendete Modell."
        )


# =========================================================
# SPRACHE ZU TEXT
# =========================================================

async def transcribe_voice(file_bytes, filename="voice.ogg"):
    if not groq_client:
        raise RuntimeError("GROQ_API_KEY fehlt")

    file_obj = (filename, file_bytes)

    transcription = groq_client.audio.transcriptions.create(
        file=file_obj,
        model=WHISPER_MODEL,
        language="de",
        response_format="text",
    )

    if isinstance(transcription, str):
        return transcription.strip()

    return getattr(transcription, "text", "").strip()


async def download_telegram_file(context, file_id):
    telegram_file = await context.bot.get_file(file_id)
    data = await telegram_file.download_as_bytearray()
    return bytes(data)


# =========================================================
# FOTO ANALYSIEREN
# =========================================================

async def analyze_photo(file_bytes, caption=""):
    if not gemini_client:
        return (
            "Ich kann Bilder gerade nicht analysieren. "
            "Bitte richte GEMINI_API_KEY bei Render ein."
        )

    try:
        image = Image.open(io.BytesIO(file_bytes))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG")
        image_data = buffer.getvalue()

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=image_data,
                    mime_type="image/jpeg",
                ),
                (
                    "Beschreibe dieses Bild auf Deutsch einfach und "
                    "freundlich. Formatiere die Antwort mit einer "
                    "fetten Überschrift und Absätzen. Nutzerfrage: "
                    + (caption or "Was ist darauf zu sehen?")
                ),
            ],
        )

        return (
            response.text
            or "Ich konnte das Bild nicht beschreiben."
        ).strip()

    except Exception:
        logger.exception("Fotoanalyse fehlgeschlagen")
        return "Beim Analysieren des Bildes ist ein Fehler aufgetreten."


# =========================================================
# NACHRICHTEN VERARBEITEN
# =========================================================

async def process_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_as_voice=False,
):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat or not text.strip():
        return

    if is_group(chat.type):
        username = await get_bot_username(context)

        if stop_requested(text):
            deactivate_group(chat.id)
            await message.reply_text(
                "Alles klar, ich bin jetzt ruhig. 🤐"
            )
            return

        addressed = addressed_to_bot(text, username)

        if addressed:
            activate_group(chat.id)
        elif not group_is_active(chat.id):
            return

    clean_text = text.strip()

    fact_match = re.match(
        r"^(?:merk dir|merke dir|merk dir bitte|bitte merke dir)\s+(.+)$",
        clean_text,
        re.IGNORECASE,
    )

    if fact_match:
        fact = fact_match.group(1).strip()
        save_fact(user.id, fact)

        answer = "Alles klar 😊 Ich merke mir das."

        save_message(chat.id, user.id, "user", clean_text)
        save_message(chat.id, user.id, "assistant", answer)

        if reply_as_voice:
            await send_voice_reply(message, answer)
        else:
            await send_long_message(message, answer)

        return

    save_message(chat.id, user.id, "user", clean_text)

    await context.bot.send_chat_action(
        chat_id=chat.id,
        action=ChatAction.TYPING,
    )

    answer = await asyncio.to_thread(
        ask_ai,
        chat.id,
        user.id,
        clean_text,
    )

    save_message(chat.id, user.id, "assistant", answer)

    if reply_as_voice:
        await send_voice_reply(message, answer)
    else:
        await send_long_message(message, answer)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message or not message.text:
        return

    await process_text_message(
        update,
        context,
        message.text,
        reply_as_voice=False,
    )


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message or not message.voice:
        return

    status = await message.reply_text(
        "🎧 Ich höre mir deine Nachricht an …"
    )

    try:
        file_bytes = await download_telegram_file(
            context,
            message.voice.file_id,
        )

        text = await transcribe_voice(file_bytes)

        if not text:
            await status.edit_text(
                "Ich konnte dich leider nicht verstehen. "
                "Versuche es bitte noch einmal. 😊"
            )
            return

        await status.edit_text("💬 Du hast gesagt: " + text[:3000])

        await process_text_message(
            update,
            context,
            text,
            reply_as_voice=True,
        )

    except Exception:
        logger.exception("Sprachnachricht fehlgeschlagen")

        await status.edit_text(
            "Die Sprachnachricht konnte ich nicht verarbeiten. "
            "Prüfe bitte den GROQ_API_KEY."
        )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.photo or not chat:
        return

    if is_group(chat.type):
        caption = message.caption or ""
        username = await get_bot_username(context)

        if (
            not addressed_to_bot(caption, username)
            and not group_is_active(chat.id)
        ):
            return

        if addressed_to_bot(caption, username):
            activate_group(chat.id)

    status = await message.reply_text(
        "👀 Ich schaue mir das Bild an …"
    )

    try:
        photo = message.photo[-1]

        file_bytes = await download_telegram_file(
            context,
            photo.file_id,
        )

        result = await asyncio.to_thread(
            lambda: asyncio.run(
                analyze_photo(file_bytes, message.caption or "")
            )
        )

        await status.delete()
        await send_long_message(message, result)

    except Exception:
        logger.exception("Foto konnte nicht verarbeitet werden")

        await status.edit_text(
            "Ich konnte das Bild leider nicht analysieren."
        )


# =========================================================
# FEHLERBEHANDLUNG
# =========================================================

async def error_handler(update, context):
    logger.error(
        "Telegram-Fehler",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ) if context.error else None,
    )


# =========================================================
# TELEGRAM-BOT STARTEN
# =========================================================

async def bot_runner():
    global bot_application

    init_db()

    bot_application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    bot_application.add_handler(
        CommandHandler("start", start_command)
    )
    bot_application.add_handler(
        CommandHandler("hilfe", help_command)
    )
    bot_application.add_handler(
        CommandHandler("bild", image_command)
    )
    bot_application.add_handler(
        CommandHandler("gedaechtnis", memory_command)
    )
    bot_application.add_handler(
        CommandHandler("vergiss", forget_command)
    )

    bot_application.add_handler(
        MessageHandler(filters.VOICE, voice_handler)
    )
    bot_application.add_handler(
        MessageHandler(filters.PHOTO, photo_handler)
    )
    bot_application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    bot_application.add_error_handler(error_handler)

    await bot_application.initialize()
    await bot_application.start()

    if bot_application.updater is None:
        raise RuntimeError("Telegram-Updater ist nicht verfügbar.")

    await bot_application.updater.start_polling(
        drop_pending_updates=False
    )

    logger.info("NEXUS Telegram-Bot ist online und lauscht.")

    try:
        await asyncio.Event().wait()
    finally:
        if bot_application.updater.running:
            await bot_application.updater.stop()

        if bot_application.running:
            await bot_application.stop()

        await bot_application.shutdown()


def bot_thread_target():
    try:
        asyncio.run(bot_runner())
    except Exception:
        logger.exception("Telegram-Bot konnte nicht gestartet werden")


def start_bot_thread():
    global bot_thread

    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN fehlt. Bitte bei Render als Environment "
            "Variable eintragen."
        )
        return

    with bot_start_lock:
        if bot_thread and bot_thread.is_alive():
            return

        bot_thread = threading.Thread(
            target=bot_thread_target,
            name="nexus-telegram",
            daemon=True,
        )

        bot_thread.start()


# Beim Laden durch Gunicorn starten.
start_bot_thread()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
