import os
import re
import html
import time
import random
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
# NEXUS KI-ASSISTENT — TELEGRAM + RENDER
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
    return "OK", 200


# ============================================================
# KONFIGURATION
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

MAX_HISTORY_MESSAGES = 12
GROUP_ACTIVE_SECONDS = 180
MAX_TELEGRAM_TEXT = 3500

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

if not TELEGRAM_TOKEN:
    logger.warning("TELEGRAM_TOKEN fehlt.")

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY fehlt. Text- und Sprach-KI sind deaktiviert.")

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY fehlt. Fotoanalyse ist deaktiviert.")

chat_history = defaultdict(list)
group_active_until = {}


# ============================================================
# SYSTEM-PROMPT
# ============================================================

SYSTEM_PROMPT = """
Du bist NEXUS, ein professioneller, freundlicher und intelligenter
deutscher KI-Assistent.

SPRACHE UND STIL:
- Antworte grundsätzlich auf Deutsch, außer der Nutzer wünscht eine andere Sprache.
- Schreibe natürlich, verständlich, freundlich und professionell.
- Beantworte zuerst die eigentliche Frage.
- Vermeide unnötige Einleitungen, Wiederholungen und Füllsätze.
- Passe die Antwortlänge an die Frage an.
- Erfinde keine Fakten und kennzeichne Unsicherheiten ehrlich.
- Schreibe übersichtlich und gut lesbar.

FORMATIERUNG:
- Verwende kurze Absätze und sinnvolle Leerzeilen.
- Verwende bei längeren Antworten klare Überschriften.
- Hebe wichtige Begriffe mit **Fettschrift** hervor.
- Nutze Aufzählungen, wenn sie die Übersicht verbessern.
- Erstelle möglichst keine Markdown-Tabellen.
- Verwende passende Emojis abhängig vom Thema.
- Setze Emojis gezielt ein, nicht wahllos.
- Bei Codeanfragen gib den vollständigen benötigten Code in Codeblöcken aus.
- Vermeide übertriebene Formatierung.

PASSENDE EMOJIS:
- Technik: 💻 ⚙️ 🛠️
- Handy: 📱
- Auto und Verkehr: 🚗 🚲
- Natur und Pflanzen: 🌱 🌳
- Essen und Kochen: 🍽️ 🍳
- Geld: 💶 💰
- Zeit: ⏰ 📅
- Ideen: 💡
- Warnungen: ⚠️
- Erfolg: ✅
- Fragen: ❓
- Bilder und Gestaltung: 🎨 🖼️
Die Auswahl ist flexibel. Verwende nur Emojis, die zum Inhalt passen.

ERKLÄRUNGEN:
- Erkläre schwierige Begriffe in verständlicher Sprache.
- Gliedere komplexe Themen in nachvollziehbare Schritte.
- Bei technischen Fragen nenne bei Bedarf Ursache, Lösung und Beispiel.
- Beende Antworten nicht automatisch mit einer unnötigen Zusammenfassung.

BILDANALYSE:
- Beschreibe nur Details, die im Bild tatsächlich erkennbar sind.
- Beginne mit dem Hauptmotiv.
- Nenne anschließend die wichtigsten Details.
- Verwende passende thematische Emojis.
- Erfinde keine Bilddetails.
- Bei Spiel-Screenshots erkläre, was erkennbar ist, welche Elemente wichtig sind
  und welche sinnvollen nächsten Schritte sich ableiten lassen.
- Bei unleserlichem Text weise darauf hin, statt ihn zu erfinden.
- Halte normale Bildbeschreibungen kompakt, sofern keine ausführliche Analyse
  verlangt wird.

Wenn jemand fragt, wer dich programmiert oder erfunden hat, antworte:
"Der liebe Heiko aus dem Schwabenländle. 😊"

Sei hilfreich, respektvoll, geduldig und ehrlich.
""".strip()


# ============================================================
# DATENBANK UND GEDÄCHTNIS
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

        logger.info("Datenbank erfolgreich initialisiert.")

    except Exception:
        logger.exception("Datenbank konnte nicht initialisiert werden.")


def save_memory(chat_id, key, value):
    key = str(key).strip()[:100]
    value = str(value).strip()[:1000]

    if not key or not value:
        return False

    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO memories (
                    chat_id, memory_key, memory_value, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, memory_key)
                DO UPDATE SET
                    memory_value = excluded.memory_value,
                    updated_at = excluded.updated_at
            """, (
                str(chat_id),
                key,
                value,
                datetime.now(timezone.utc).isoformat(),
            ))

        return True

    except Exception:
        logger.exception("Speichern der Erinnerung fehlgeschlagen.")
        return False


def get_memories(chat_id, limit=30):
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT memory_key, memory_value
                FROM memories
                WHERE chat_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """, (str(chat_id), limit)).fetchall()

        return rows

    except Exception:
        logger.exception("Lesen der Erinnerungen fehlgeschlagen.")
        return []


def forget_memory(chat_id, key):
    try:
        with db_connect() as conn:
            cursor = conn.execute("""
                DELETE FROM memories
                WHERE chat_id = ?
                  AND memory_key LIKE ?
            """, (str(chat_id), f"%{key}%"))

            return cursor.rowcount

    except Exception:
        logger.exception("Löschen der Erinnerung fehlgeschlagen.")
        return 0


def log_message(chat_id, role, content):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO conversation_log (
                    chat_id, role, content, created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                str(chat_id),
                role,
                str(content)[:4000],
                datetime.now(timezone.utc).isoformat(),
            ))

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
                return f"Alles klar, ich merke mir: {key} – {value} 😊"

    return None


# ============================================================
# MARKDOWN SICHER IN TELEGRAM-HTML UMWANDELN
# ============================================================

def markdown_to_telegram_html(text):
    """
    Wandelt gängige Markdown-Formatierung in Telegram-HTML um.
    Benutzereingaben und KI-Ausgaben werden HTML-escaped,
    bevor Formatierungs-Tags eingesetzt werden.
    """

    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # Markdown-Tabellen in lesbare Listen umwandeln.
    lines = text.split("\n")
    converted_lines = []
    index = 0

    while index < len(lines):
        line = lines[index]

        if (
            line.count("|") >= 2
            and index + 1 < len(lines)
        ):
            separator = lines[index + 1].strip()

            is_separator = (
                separator.count("|") >= 2
                and re.fullmatch(r"[|\s:-]+", separator) is not None
                and "-" in separator
            )

            if is_separator:
                headers = [
                    cell.strip()
                    for cell in line.strip().strip("|").split("|")
                ]

                index += 2

                while (
                    index < len(lines)
                    and lines[index].count("|") >= 2
                ):
                    cells = [
                        cell.strip()
                        for cell in lines[index].strip().strip("|").split("|")
                    ]

                    if any(cells):
                        parts = []

                        for cell_index, cell in enumerate(cells):
                            if not cell:
                                continue

                            label = (
                                headers[cell_index]
                                if cell_index < len(headers)
                                else ""
                            )

                            parts.append(
                                f"{label}: {cell}" if label else cell
                            )

                        if parts:
                            converted_lines.append("• " + " — ".join(parts))

                    index += 1

                continue

        converted_lines.append(line)
        index += 1

    text = "\n".join(converted_lines)

    # Codeblöcke vorübergehend schützen.
    code_blocks = []

    def save_code_block(match):
        code = html.escape(match.group(2), quote=False)
        code = f"<pre>{code}</pre>"

        code_blocks.append(code)
        return f"NEXUSCODEPLACEHOLDER{len(code_blocks) - 1}END"

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```",
        save_code_block,
        text,
        flags=re.S,
    )

    # Restlichen Text escapen, damit Telegram-HTML gültig bleibt.
    text = html.escape(text, quote=False)

    # Horizontale Trennlinien entfernen.
    text = re.sub(
        r"(?m)^\s*(?:---+|___+|\*\*\*+)\s*$",
        "",
        text,
    )

    # Überschriften.
    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$",
        r"<b>\1</b>",
        text,
    )

    # Inline-Code.
    text = re.sub(
        r"`([^`\n]+)`",
        r"<code>\1</code>",
        text,
    )

    # Fett.
    text = re.sub(
        r"\*\*([^\n]+?)\*\*",
        r"<b>\1</b>",
        text,
    )

    # Unterstrichen.
    text = re.sub(
        r"__([^\n]+?)__",
        r"<u>\1</u>",
        text,
    )

    # Kursiv.
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    # Listenpunkte.
    text = re.sub(
        r"(?m)^\s*[-*]\s+",
        "• ",
        text,
    )

    # Geschützte Codeblöcke wieder einsetzen.
    for block_index, block in enumerate(code_blocks):
        text = text.replace(
            f"NEXUSCODEPLACEHOLDER{block_index}END",
            block,
        )

    return text.strip() or "Entschuldigung, ich habe gerade keine Antwort."


async def send_long_text(message, text):
    """
    Sendet längere Antworten in Telegram-kompatiblen Abschnitten.
    Bei ungültigem HTML wird auf Klartext zurückgegriffen.
    """

    text = text or "Entschuldigung, ich habe gerade keine Antwort."

    chunks = []
    remaining = text

    while len(remaining) > MAX_TELEGRAM_TEXT:
        split_at = remaining.rfind("\n", 0, MAX_TELEGRAM_TEXT)

        if split_at < 1000:
            split_at = MAX_TELEGRAM_TEXT

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    for chunk in chunks:
        formatted = markdown_to_telegram_html(chunk)

        try:
            await message.reply_text(
                formatted,
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Telegram-HTML konnte nicht gesendet werden; nutze Klartext."
            )

            try:
                await message.reply_text(chunk[:4000])

            except Exception:
                logger.exception("Auch das Senden als Klartext ist fehlgeschlagen.")


# ============================================================
# KI-ANTWORTEN ÜBER GROQ
# ============================================================

def get_ai_reply(chat_id, user_text):
    if not groq_client:
        return (
            "⚠️ Meine Text-KI ist noch nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY in den Render-Umgebungsvariablen."
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

    messages.extend(chat_history[str(chat_id)][-MAX_HISTORY_MESSAGES:])
    messages.append({
        "role": "user",
        "content": user_text[:6000],
    })

    try:
        result = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=900,
        )

        answer = (
            result.choices[0].message.content
            or "Ich habe gerade keine Antwort."
        ).strip()

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

        chat_history[str(chat_id)] = history[-MAX_HISTORY_MESSAGES:]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen.")

        return (
            "⚠️ Meine KI hat gerade ein Problem. "
            "Bitte prüfe GROQ_API_KEY, GROQ_MODEL und die Groq-Dienstverfügbarkeit."
        )


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Hallo! Ich bin NEXUS, dein KI-Assistent. 🤖\n\n"
        "Schreib mir einfach eine Nachricht oder sende mir ein Bild. 🖼️\n\n"
        "Meine Befehle:\n"
        "/start – Begrüßung und Hilfe\n"
        "/merken Text – Erinnerung speichern\n"
        "/gedaechtnis – gespeicherte Erinnerungen anzeigen\n"
        "/vergiss Text – Erinnerung löschen\n"
        "/reset – kurzen Chat-Verlauf zurücksetzen\n"
        "/bild Beschreibung – Bild erstellen\n\n"
        "Du kannst mir auch eine Sprachnachricht schicken. 🎧"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    chat_history.pop(chat_id, None)

    await update.effective_message.reply_text(
        "Der kurze Chat-Verlauf wurde zurückgesetzt. 🧹\n"
        "Gespeicherte Erinnerungen bleiben erhalten."
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Schreib bitte dazu, was ich mir merken soll.\n"
            "Beispiel: /merken Hobby: Wandern"
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
            "⚠️ Ich konnte das nicht speichern. "
            "Bitte prüfe den Datenbankpfad und die Render-Konfiguration."
        )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memories = get_memories(update.effective_chat.id)

    if not memories:
        await update.effective_message.reply_text(
            "Ich habe hier noch keine gespeicherten Erinnerungen. 🧠"
        )
        return

    text = "🧠 **Das habe ich gespeichert:**\n\n" + "\n".join(
        f"• **{key}:** {value}"
        for key, value in memories
    )

    await send_long_text(update.effective_message, text)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = " ".join(context.args).strip()

    if not key:
        await update.effective_message.reply_text(
            "Schreib bitte dazu, was ich vergessen soll.\n"
            "Beispiel: /vergiss Hobby"
        )
        return

    count = forget_memory(update.effective_chat.id, key)

    if count:
        await update.effective_message.reply_text(
            "Die passende Erinnerung wurde gelöscht. 🧹"
        )
    else:
        await update.effective_message.reply_text(
            "Ich habe dazu keine passende Erinnerung gefunden."
        )


# ============================================================
# BILDER ERSTELLEN
# ============================================================

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    message = update.effective_message

    if not prompt:
        await message.reply_text(
            "Bitte gib eine Bildbeschreibung an.\n"
            "Beispiel: /bild ein leuchtender Energieplanet im Weltall"
        )
        return

    await message.reply_text(
        "Ich versuche, dein Bild zu erstellen. 🎨"
    )

    temp_path = None

    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            + requests.utils.quote(prompt)
        )

        response = await asyncio.to_thread(
            lambda: requests.get(
                url,
                params={
                    "width": 1024,
                    "height": 1024,
                    "nologo": "true",
                },
                timeout=(15, 90),
            )
        )

        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()

        if "image" not in content_type:
            raise RuntimeError(
                "Der Bilddienst hat keine Bilddatei zurückgegeben."
            )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as tmp:
            tmp.write(response.content)
            temp_path = tmp.name

        with open(temp_path, "rb") as photo:
            await message.reply_photo(
                photo=photo,
                caption=prompt[:900],
            )

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen.")

        await message.reply_text(
            "⚠️ Das Bild konnte gerade nicht erstellt werden. "
            "Der Bilddienst könnte vorübergehend nicht erreichbar sein. "
            "Bitte versuche es später erneut."
        )

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ============================================================
# SPRACHNACHRICHTEN ERKENNEN
# ============================================================

async def transcribe_voice(file_path):
    if not groq_client:
        return None

    def transcribe():
        with open(file_path, "rb") as audio_file:
            audio_data = audio_file.read()

        return groq_client.audio.transcriptions.create(
            file=(
                os.path.basename(file_path),
                audio_data,
            ),
            model=WHISPER_MODEL,
            response_format="text",
            language="de",
        )

    transcription = await asyncio.to_thread(transcribe)

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
                suffix=".mp3",
                delete=False,
            ) as tmp:
                voice_path = tmp.name

            if await make_voice_file(text, voice_path):
                with open(voice_path, "rb") as audio:
                    await message.reply_voice(voice=audio)

                return

        except Exception:
            logger.exception("Senden der Sprachantwort fehlgeschlagen.")

        finally:
            if voice_path:
                try:
                    os.remove(voice_path)
                except OSError:
                    pass

    await send_long_text(message, text)


# ============================================================
# GRUPPENSTEUERUNG
# ============================================================

def is_group(chat_type):
    return chat_type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    )


def is_addressed(text, bot_username=""):
    low = (text or "").lower().strip()
    username = (bot_username or "").lower().lstrip("@")

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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.text:
        return

    text = message.text.strip()
    group = is_group(chat.type)
    now = asyncio.get_running_loop().time()

    if group:
        bot_username = context.bot.username or ""
        addressed = is_addressed(text, bot_username)

        if is_stop_phrase(text) and (
            addressed or now < group_active_until.get(chat.id, 0)
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

    try:
        await message.chat.send_action("typing")
    except Exception:
        logger.debug("Typing-Status konnte nicht gesendet werden.")

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

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.voice:
        return

    group = is_group(chat.type)
    now = asyncio.get_running_loop().time()

    if group and now >= group_active_until.get(chat.id, 0):
        await message.reply_text(
            "Sprich mich bitte zuerst mit „Nexus“ an. 😊"
        )
        return

    if not groq_client:
        await message.reply_text(
            "⚠️ Meine Spracherkennung ist nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY."
        )
        return

    await message.reply_text("Ich höre zu … 🎧")

    file_path = None

    try:
        telegram_file = await context.bot.get_file(
            message.voice.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False,
        ) as tmp:
            file_path = tmp.name

        await telegram_file.download_to_drive(file_path)

        spoken_text = await transcribe_voice(file_path)

        if not spoken_text:
            await message.reply_text(
                "Ich konnte die Sprachnachricht leider nicht verstehen. 🎧"
            )
            return

        learned = learn_from_text(chat.id, spoken_text)

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
            "⚠️ Beim Verarbeiten der Sprachnachricht ist ein Fehler aufgetreten. "
            "Bitte versuche es erneut."
        )

    finally:
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass


# ============================================================
# GEMINI-FOTOANALYSE
# ============================================================

def is_retryable_gemini_error(exc):
    """
    Erkennt vorübergehende Fehler, bei denen ein erneuter Versuch
    sinnvoll sein kann.
    """

    error_text = str(exc).lower()

    retry_markers = (
        "503",
        "unavailable",
        "high demand",
        "overloaded",
        "temporarily unavailable",
        "500",
        "internal server error",
        "429",
        "resource exhausted",
        "rate limit",
        "deadline exceeded",
        "timed out",
        "timeout",
        "connection reset",
        "connection error",
    )

    return any(
        marker in error_text
        for marker in retry_markers
    )


def generate_gemini_content(client, types, prompt, image_part):
    """
    Synchroner Gemini-Aufruf. Er läuft in einem Worker-Thread,
    damit die Telegram-Ereignisschleife nicht blockiert wird.
    """

    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            prompt,
            image_part,
        ],
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=600,
        ),
    )


async def generate_gemini_with_retries(
    client,
    types,
    prompt,
    image_part,
    attempts=3,
):
    """
    Wiederholt nur Aufrufe, bei denen ein vorübergehender Fehler
    erkannt wurde. Bei ungültigen Schlüsseln oder fehlenden
    Berechtigungen wird nicht unnötig weiterprobiert.
    """

    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            logger.info(
                "Gemini-Fotoanalyse: Versuch %s von %s; Modell=%s",
                attempt,
                attempts,
                GEMINI_MODEL,
            )

            result = await asyncio.to_thread(
                generate_gemini_content,
                client,
                types,
                prompt,
                image_part,
            )

            return result

        except Exception as exc:
            last_error = exc

            if (
                attempt >= attempts
                or not is_retryable_gemini_error(exc)
            ):
                raise

            # Kurze, ansteigende Wartezeit mit kleinem Zufallsanteil.
            delay = min(2 ** attempt, 8) + random.uniform(0.1, 0.7)

            logger.warning(
                "Vorübergehender Gemini-Fehler bei Versuch %s: %s. "
                "Neuer Versuch in %.1f Sekunden.",
                attempt,
                type(exc).__name__,
                delay,
            )

            await asyncio.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError("Die Gemini-Anfrage konnte nicht abgeschlossen werden.")


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

    caption = (
        message.caption
        or "Beschreibe dieses Bild auf Deutsch in verständlichen Worten."
    )

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

    temp_path = None
    client = None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=GEMINI_API_KEY,
        )

        # Telegrams größtes verfügbares Foto verwenden.
        telegram_file = await context.bot.get_file(
            message.photo[-1].file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as tmp:
            temp_path = tmp.name

        await telegram_file.download_to_drive(temp_path)

        with open(temp_path, "rb") as image_file:
            image_bytes = image_file.read()

        if not image_bytes:
            raise RuntimeError("Die heruntergeladene Bilddatei ist leer.")

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        )

        photo_prompt = f"""
Analysiere das angehängte Bild sorgfältig und antworte auf Deutsch.

AUFGABE DES NUTZERS:
{caption}

FORMAT:
- Beginne mit einer passenden, kurzen Überschrift und einem thematischen Emoji.
- Beschreibe zuerst das Hauptmotiv in einem Satz.
- Nenne die wichtigsten Details in höchstens 5 Stichpunkten.
- Nutze passende Emojis für tatsächlich erkennbare Dinge.
- Verwende kurze Absätze und eine professionelle, gut lesbare Formatierung.
- Verwende bei Bedarf **Fettschrift**.
- Erfinde keine Details, die im Bild nicht erkennbar sind.
- Wenn Text nicht lesbar ist, sage das ausdrücklich.
- Bei einem Spiel-Screenshot: Erkläre kurz das erkennbare Spielziel,
  wichtige Elemente und mögliche sinnvolle nächste Schritte.
- Bei technischen Bildern: Erkläre erkennbare Komponenten und deren Funktion,
  soweit dies aus dem Bild zuverlässig hervorgeht.
- Antworte normalerweise mit höchstens 150 Wörtern.
- Wenn der Nutzer ausdrücklich eine ausführliche Analyse verlangt,
  darf die Antwort länger sein.
- Beantworte die konkrete Frage des Nutzers, falls eine gestellt wurde.
"""

        logger.info(
            "Starte Fotoanalyse; Modell=%s, Chat=%s",
            GEMINI_MODEL,
            chat.id,
        )

        # Die Wiederholungen fangen vorübergehende 503-/429-Fehler ab.
        # Ein dauerhafter API-Ausfall kann trotzdem nicht verhindert werden.
        result = await asyncio.wait_for(
            generate_gemini_with_retries(
                client=client,
                types=types,
                prompt=photo_prompt,
                image_part=image_part,
                attempts=3,
            ),
            timeout=75,
        )

        answer = getattr(result, "text", None)

        if not answer or not answer.strip():
            await message.reply_text(
                "⚠️ Gemini hat keine verwertbare Bildbeschreibung zurückgegeben. "
                "Bitte versuche es erneut."
            )
            return

        await send_long_text(
            message,
            answer.strip(),
        )

        logger.info(
            "Fotoanalyse erfolgreich abgeschlossen; Chat=%s",
            chat.id,
        )

    except asyncio.TimeoutError:
        logger.warning(
            "Zeitlimit der Fotoanalyse erreicht; Chat=%s",
            chat.id,
        )

        await message.reply_text(
            "⏳ Die Bildanalyse dauert gerade zu lange. "
            "Der Bilddienst könnte stark ausgelastet sein. "
            "Bitte versuche es später erneut."
        )

    except Exception as exc:
        logger.exception("Fotoanalyse fehlgeschlagen.")

        error_text = str(exc).lower()

        if (
            "api key" in error_text
            or "api_key" in error_text
            or "invalid_argument" in error_text
            or "unauthorized" in error_text
        ):
            user_message = (
                "🔑 Die Gemini-Anfrage wurde abgelehnt. "
                "Bitte prüfe GEMINI_API_KEY und die API-Konfiguration in Render."
            )

        elif "404" in error_text or "not found" in error_text:
            user_message = (
                f"⚙️ Das Gemini-Modell „{GEMINI_MODEL}“ wurde nicht gefunden "
                "oder ist für deinen Schlüssel nicht verfügbar. "
                "Prüfe GEMINI_MODEL und den Modellzugriff."
            )

        elif (
            "403" in error_text
            or "permission" in error_text
            or "forbidden" in error_text
        ):
            user_message = (
                "🔒 Gemini hat den Zugriff verweigert. "
                "Bitte prüfe API-Berechtigungen und Projektzugriff."
            )

        elif (
            "429" in error_text
            or "quota" in error_text
            or "rate limit" in error_text
            or "resource exhausted" in error_text
        ):
            user_message = (
                "⏳ Das Gemini-Limit oder die aktuelle Anfragerate wurde erreicht. "
                "Bitte warte etwas und versuche es erneut."
            )

        elif (
            "503" in error_text
            or "unavailable" in error_text
            or "high demand" in error_text
            or "overloaded" in error_text
        ):
            user_message = (
                "🔄 Der Gemini-Bilddienst ist momentan stark ausgelastet "
                "oder vorübergehend nicht verfügbar. "
                "Ich habe es erneut versucht, aber der Dienst antwortet noch nicht. "
                "Bitte versuche es später erneut."
            )

        elif (
            "timeout" in error_text
            or "timed out" in error_text
        ):
            user_message = (
                "⏳ Die Bildanalyse hat das Zeitlimit überschritten. "
                "Bitte versuche es mit dem Bild später erneut."
            )

        else:
            user_message = (
                "⚠️ Die Fotoanalyse ist fehlgeschlagen. "
                "Bitte versuche es erneut. Weitere technische Details stehen "
                "in den Render Runtime Logs."
            )

        await message.reply_text(user_message)

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug(
                    "Gemini-Client konnte nicht geschlossen werden.",
                    exc_info=True,
                )


# ============================================================
# FEHLERBEHANDLUNG
# ============================================================

async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    if context.error:
        logger.error(
            "Telegram-Fehler:",
            exc_info=(
                type(context.error),
                context.error,
                context.error.__traceback__,
            ),
        )
    else:
        logger.error("Telegram-Fehler ohne weitere Details.")


# ============================================================
# TELEGRAM-APPLICATION EINRICHTEN
# ============================================================

def build_application():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN fehlt. Bitte in Render konfigurieren."
        )

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

    return application


# ============================================================
# TELEGRAM-BOT ASYNCHRON STARTEN
# ============================================================

async def run_bot_async():
    application = build_application()
    updater = application.updater

    if updater is None:
        raise RuntimeError(
            "Der Telegram-Updater konnte nicht erstellt werden."
        )

    initialized = False
    started = False
    polling_started = False

    try:
        logger.info("Initialisiere Telegram-Anwendung …")

        await application.initialize()
        initialized = True

        await application.start()
        started = True

        await updater.start_polling(
            drop_pending_updates=False,
        )
        polling_started = True

        logger.info(
            "NEXUS Telegram-Bot läuft und empfängt Nachrichten."
        )

        await asyncio.Event().wait()

    finally:
        logger.info("Telegram-Bot wird heruntergefahren …")

        if polling_started:
            try:
                await updater.stop()
            except Exception:
                logger.exception(
                    "Fehler beim Stoppen des Telegram-Updaters."
                )

        if started:
            try:
                await application.stop()
            except Exception:
                logger.exception(
                    "Fehler beim Stoppen der Telegram-Anwendung."
                )

        if initialized:
            try:
                await application.shutdown()
            except Exception:
                logger.exception(
                    "Fehler beim Herunterfahren der Telegram-Anwendung."
                )


def main():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error(
            "Bot startet nicht: TELEGRAM_TOKEN fehlt."
        )
        return

    logger.info("NEXUS Telegram-Bot startet.")
    asyncio.run(run_bot_async())


# ============================================================
# BOT IM HINTERGRUND STARTEN
# ============================================================

_bot_thread = None
_bot_lock = threading.Lock()


def start_bot_thread():
    global _bot_thread

    with _bot_lock:
        if _bot_thread and _bot_thread.is_alive():
            logger.info(
                "Telegram-Bot-Thread läuft bereits."
            )
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

        logger.info(
            "Telegram-Bot-Thread wurde gestartet."
        )


# ============================================================
# ANWENDUNG INITIALISIEREN
# ============================================================

init_db()
start_bot_thread()
