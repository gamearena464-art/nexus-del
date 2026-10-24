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
# NEXUS KI-ASSISTENT — RENDER + TELEGRAM
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

if not TELEGRAM_TOKEN:
    logger.warning("TELEGRAM_TOKEN fehlt.")

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY fehlt.")

if not GEMINI_API_KEY:
    logger.warning(
        "GEMINI_API_KEY fehlt; Fotoanalyse ist deaktiviert."
    )

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ============================================================
# SYSTEM-PROMPT
# ============================================================

SYSTEM_PROMPT = """
Du bist NEXUS, ein professioneller und freundlicher deutscher
KI-Assistent.

Antworte auf Deutsch, verständlich, korrekt und übersichtlich.

FORMATIERUNG:
- Nutze passende Emojis, wenn sie zum Thema passen.
- Beginne längere Antworten mit einer kurzen Überschrift.
- Gliedere längere Antworten in sinnvolle Abschnitte.
- Verwende zwischen Abschnitten eine Leerzeile.
- Verwende **Fettdruck** für Überschriften und wichtige Begriffe.
- Verwende Aufzählungen statt Tabellen.
- Erstelle keine Markdown-Tabellen.
- Vermeide riesige, unübersichtliche Textblöcke.
- Halte Absätze kurz und verständlich.
- Verwende Nummerierungen für Schritt-für-Schritt-Anleitungen.
- Bei technischen Erklärungen beschreibe Funktion und Zweck.
- Verwende Codeblöcke nur für tatsächlichen Programmcode.
- Verwende keine HTML-Tags in deinen Antworten.
- Schreibe natürliche, grammatikalisch korrekte Sätze.
- Erhalte sinnvolle Abstände und Zeilenumbrüche.
- Erfinde keine Fakten und kennzeichne Unsicherheiten.

BEISPIEL FÜR EINE GUTE STRUKTUR:

**🔧 Aufgabe eines Bauteils**

Das Bauteil übernimmt eine bestimmte Funktion im System.

**⚙️ Funktionsweise**
- Erster wichtiger Punkt.
- Zweiter wichtiger Punkt.
- Dritter wichtiger Punkt.

**💡 Gut zu wissen**
Ein zusätzlicher hilfreicher Hinweis.

Wenn jemand fragt, wer dich programmiert oder erfunden hat,
antworte:
"Der liebe Heiko aus dem Schwabenländle. 😊"

Sei freundlich, geduldig und respektvoll.
""".strip()


chat_history = defaultdict(list)
group_active_until = {}

MAX_HISTORY_MESSAGES = 12
GROUP_ACTIVE_SECONDS = 180


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

    except Exception:
        logger.exception(
            "Datenbank konnte nicht initialisiert werden."
        )


def save_memory(chat_id, key, value):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO memories(
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
                WHERE chat_id=? AND memory_key LIKE ?
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
                INSERT INTO conversation_log(
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


# ============================================================
# MARKDOWN IN TELEGRAM-HTML UMWANDELN
# ============================================================

def markdown_to_telegram_html(text):
    """
    Wandelt Markdown in Telegram-kompatibles HTML um.

    Erhält:
    - Leerzeilen und Absätze
    - Emojis
    - Überschriften
    - Fettdruck und Kursivschrift
    - Aufzählungen und Nummerierungen
    - Inline-Code und Codeblöcke

    HTML wird maskiert, damit Telegram keine ungültigen Tags erhält.
    """

    if not isinstance(text, str) or not text.strip():
        return "Entschuldigung, ich habe gerade keine Antwort."

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")

    # --------------------------------------------------------
    # Markdown-Tabellen in lesbare Listen umwandeln
    # --------------------------------------------------------

    lines = text.split("\n")
    converted_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        is_table = (
            "|" in line
            and i + 1 < len(lines)
            and re.fullmatch(
                r"\s*\|?\s*:?-{2,}:?\s*"
                r"(?:\|\s*:?-{2,}:?\s*)+\|?\s*",
                lines[i + 1],
            ) is not None
        )

        if not is_table:
            converted_lines.append(line)
            i += 1
            continue

        headers = [
            cell.strip()
            for cell in line.strip().strip("|").split("|")
        ]

        i += 2

        while i < len(lines) and "|" in lines[i]:
            cells = [
                cell.strip()
                for cell in lines[i].strip().strip("|").split("|")
            ]

            if any(cells):
                parts = []

                for j, cell in enumerate(cells):
                    if not cell:
                        continue

                    if j < len(headers) and headers[j]:
                        parts.append(f"{headers[j]}: {cell}")
                    else:
                        parts.append(cell)

                if parts:
                    converted_lines.append("• " + " — ".join(parts))

            i += 1

        converted_lines.append("")

    text = "\n".join(converted_lines)

    # --------------------------------------------------------
    # Codeblöcke schützen
    # --------------------------------------------------------

    code_blocks = []

    def save_code_block(match):
        code = html.escape(match.group(2), quote=False)
        placeholder = f"NEXUSCODEPLACEHOLDER{len(code_blocks)}END"

        code_blocks.append(f"<pre>{code}</pre>")

        return placeholder

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```",
        save_code_block,
        text,
        flags=re.DOTALL,
    )

    # --------------------------------------------------------
    # HTML maskieren, bevor Formatierung eingefügt wird
    # --------------------------------------------------------

    text = html.escape(text, quote=False)

    # Horizontale Trennlinien entfernen.
    text = re.sub(
        r"(?m)^[ \t]*(?:-{3,}|_{3,}|\*{3,})[ \t]*$",
        "",
        text,
    )

    # --------------------------------------------------------
    # Überschriften
    # --------------------------------------------------------

    text = re.sub(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$",
        r"<b>\1</b>",
        text,
    )

    # --------------------------------------------------------
    # Inline-Code
    # --------------------------------------------------------

    text = re.sub(
        r"`([^`\n]+)`",
        r"<code>\1</code>",
        text,
    )

    # --------------------------------------------------------
    # Fett
    # --------------------------------------------------------

    text = re.sub(
        r"(?<!\*)\*\*(.+?)\*\*(?!\*)",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(
        r"(?<!_)__(.+?)__(?!_)",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    # --------------------------------------------------------
    # Kursiv
    # --------------------------------------------------------

    text = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    text = re.sub(
        r"(?<![A-Za-z0-9])_([^_\n]+?)_(?![A-Za-z0-9])",
        r"<i>\1</i>",
        text,
    )

    # --------------------------------------------------------
    # Aufzählungen
    # --------------------------------------------------------

    text = re.sub(
        r"(?m)^[ \t]*[-*+][ \t]+",
        "• ",
        text,
    )

    # Nummerierte Listen.
    text = re.sub(
        r"(?m)^[ \t]*(\d+)\.[ \t]+",
        r"\1. ",
        text,
    )

    # Mehr als zwei aufeinanderfolgende Leerzeilen reduzieren.
    # Normale Zeilenumbrüche bleiben erhalten.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Geschützte Codeblöcke wiederherstellen.
    for index, block in enumerate(code_blocks):
        text = text.replace(
            f"NEXUSCODEPLACEHOLDER{index}END",
            block,
        )

    return text.strip() or "Entschuldigung, ich habe gerade keine Antwort."


async def send_long_text(message, text, parse_mode=None):
    """
    Sendet Nachrichten mit möglichst intakter Formatierung.
    Teilt lange Antworten möglichst an Absatzgrenzen.
    """

    if not isinstance(text, str) or not text.strip():
        text = "Entschuldigung, ich habe gerade keine Antwort."

    max_length = 3500
    chunks = []
    remaining = text.strip()

    while len(remaining) > max_length:
        # Zuerst nach einem Absatzumbruch suchen.
        split_at = remaining.rfind("\n\n", 0, max_length)

        # Wenn kein sinnvoller Absatzumbruch existiert:
        if split_at < max_length // 3:
            split_at = remaining.rfind("\n", 0, max_length)

        # Wenn keine sinnvolle Zeilengrenze existiert:
        if split_at < max_length // 3:
            split_at = remaining.rfind(" ", 0, max_length)

        # Notfalls an der maximalen Länge teilen.
        if split_at < max_length // 3:
            split_at = max_length

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    for chunk in chunks:
        formatted = markdown_to_telegram_html(chunk)

        try:
            await message.reply_text(
                text=formatted,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        except Exception:
            logger.exception(
                "Telegram-HTML fehlgeschlagen; sende Klartext."
            )

            # Sicherer Ersatz, falls Telegram das HTML ablehnt.
            plain = re.sub(
                r"</?(?:b|i|u|s|code|pre)>",
                "",
                formatted,
            )
            plain = html.unescape(plain)

            await message.reply_text(
                text=plain[:4000],
                disable_web_page_preview=True,
            )


# ============================================================
# KI-ANTWORTEN
# ============================================================

def get_ai_reply(chat_id, user_text):
    if not groq_client:
        return (
            "Meine KI ist noch nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY in Render."
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
            max_tokens=900,
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

        chat_history[str(chat_id)] = history[-MAX_HISTORY_MESSAGES:]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen.")

        return (
            "Ups, meine KI hat gerade ein Problem. "
            "Bitte prüfe GROQ_API_KEY und GROQ_MODEL in Render."
        )


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "Hallo! Ich bin NEXUS, dein KI-Assistent. 🤖😊\n\n"
        "Schreib mir einfach eine Nachricht.\n\n"
        "Befehle:\n"
        "/merken Text – etwas speichern\n"
        "/gedaechtnis – Erinnerungen ansehen\n"
        "/vergiss Text – Erinnerung löschen\n"
        "/reset – Chat-Verlauf zurücksetzen\n"
        "/bild Beschreibung – Bild erstellen"
    )


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

    text = "🧠 Das habe ich gespeichert:\n" + "\n".join(
        f"• {key}: {value}"
        for key, value in memories
    )

    await send_long_text(update.effective_message, text)


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

    count = forget_memory(update.effective_chat.id, key)

    if count:
        await update.effective_message.reply_text(
            "Erinnerung gelöscht. 🧹"
        )
    else:
        await update.effective_message.reply_text(
            "Ich habe dazu keine passende Erinnerung gefunden."
        )


# ============================================================
# BILDER ERSTELLEN
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
                timeout=90,
            )
        )

        response.raise_for_status()

        if "image" not in response.headers.get("content-type", ""):
            raise RuntimeError(
                "Der Bilddienst hat kein Bild zurückgegeben."
            )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as tmp:
            tmp.write(response.content)
            temp_path = tmp.name

        try:
            with open(temp_path, "rb") as photo:
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=prompt[:900],
                )

        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen.")

        await update.effective_message.reply_text(
            "Das Bild konnte gerade nicht erstellt werden. "
            "Bitte versuche es später erneut."
        )


# ============================================================
# SPRACHNACHRICHTEN
# ============================================================

async def transcribe_voice(file_path):
    if not groq_client:
        return None

    def transcribe():
        with open(file_path, "rb") as audio_file:
            return groq_client.audio.transcriptions.create(
                file=(
                    os.path.basename(file_path),
                    audio_file.read(),
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

        if is_stop_phrase(text) and (
            addressed
            or now < group_active_until.get(chat.id, 0)
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

    await message.chat.send_action("typing")

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

    if group and now >= group_active_until.get(chat.id, 0):
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

        spoken_text = await transcribe_voice(file_path)

        if not spoken_text:
            await message.reply_text(
                "Ich konnte die Sprachnachricht leider nicht verstehen."
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
            "Beim Verarbeiten der Sprachnachricht ist ein Fehler passiert."
        )

    finally:
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass


# ============================================================
# FOTOS ANALYSIEREN — GEMINI
# ============================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.photo:
        return

    if not GEMINI_API_KEY:
        await message.reply_text(
            "Für die Fotoanalyse fehlt GEMINI_API_KEY in Render."
        )
        return

    caption = (
        message.caption
        or "Beschreibe dieses Bild auf Deutsch in einfachen Worten."
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

    path = None
    client = None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=GEMINI_API_KEY,
        )

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

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        )

        logger.info(
            "Fotoanalyse gestartet; Modell=%s, Chat=%s",
            GEMINI_MODEL,
            chat.id,
        )

        result = await asyncio.to_thread(
            lambda: client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[caption, image_part],
            )
        )

        answer = getattr(result, "text", None)

        if not answer:
            answer = (
                "Ich konnte das Bild nicht beschreiben. "
                "Bitte versuche es erneut."
            )

        await send_long_text(message, answer)

        logger.info(
            "Fotoanalyse erfolgreich abgeschlossen; Chat=%s",
            chat.id,
        )

    except Exception as exc:
        logger.exception("Fotoanalyse fehlgeschlagen.")

        error_text = str(exc).lower()

        if (
            "api key" in error_text
            or "api_key" in error_text
            or "unauthorized" in error_text
        ):
            user_message = (
                "Die Gemini-Anmeldung ist fehlgeschlagen. "
                "Bitte prüfe GEMINI_API_KEY in Render."
            )

        elif "404" in error_text or "not found" in error_text:
            user_message = (
                f"Das Gemini-Modell „{GEMINI_MODEL}“ wurde nicht "
                "gefunden oder ist für deinen Schlüssel nicht verfügbar. "
                "Bitte prüfe Modellname und API-Zugriff."
            )

        elif (
            "403" in error_text
            or "permission" in error_text
            or "forbidden" in error_text
        ):
            user_message = (
                "Gemini hat den Zugriff verweigert. "
                "Bitte prüfe API-Berechtigungen und Projektzugriff."
            )

        elif (
            "429" in error_text
            or "quota" in error_text
            or "rate limit" in error_text
        ):
            user_message = (
                "Das Gemini-Limit wurde erreicht. "
                "Bitte warte etwas und versuche es erneut."
            )

        else:
            user_message = (
                "Die Fotoanalyse ist fehlgeschlagen. "
                "Die genaue Ursache steht in den Render Runtime Logs."
            )

        await message.reply_text(user_message)

    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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
        logger.error(
            "Telegram-Fehler ohne weitere Details."
        )


# ============================================================
# TELEGRAM-APPLICATION EINRICHTEN
# ============================================================

def build_application():
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
        logger.info(
            "Initialisiere Telegram-Anwendung …"
        )

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
        logger.info(
            "Telegram-Bot wird heruntergefahren …"
        )

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
