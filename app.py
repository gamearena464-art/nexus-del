import os
import re
import html
import base64
import logging
import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
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
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()

GEMINI_FALLBACK_MODEL = os.getenv(
    "GEMINI_FALLBACK_MODEL", "gemini-2.5-flash"
).strip()

# ============================================================
# GEMINI-FOTOANALYSE
# ============================================================

GEMINI_ATTEMPT_TIMEOUT = 45
GEMINI_MAX_ATTEMPTS_PER_MODEL = 2
GEMINI_ANALYSIS_TIMEOUT = 220
GEMINI_PHOTO_MAX_OUTPUT_TOKENS = 1500
GEMINI_PHOTO_MIN_WORDS = 180
GEMINI_PHOTO_TARGET_WORDS = 300

# ============================================================
# AI HORDE — BILDER ERSTELLEN UND BEARBEITEN
# ============================================================

AI_HORDE_API_KEY = os.getenv("AI_HORDE_API_KEY", "").strip()
AI_HORDE_API = "https://aihorde.net/api/v2"
AI_HORDE_CLIENT_AGENT = "NEXUS-Telegram-Bot:1.0"

AI_HORDE_TIMEOUT_SECONDS = 600
AI_HORDE_IMAGE_WIDTH = 512
AI_HORDE_IMAGE_HEIGHT = 512

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
    logger.warning("GEMINI_API_KEY fehlt; Fotoanalyse ist deaktiviert.")

if not AI_HORDE_API_KEY:
    logger.warning("AI_HORDE_API_KEY fehlt; Bilderstellung ist deaktiviert.")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """
Du bist NEXUS, ein professioneller, freundlicher und intelligenter deutscher KI-Assistent.

SPRACHE UND STIL:
- Antworte grundsätzlich auf Deutsch, außer der Nutzer wünscht eine andere Sprache.
- Schreibe natürlich, verständlich, freundlich und professionell.
- Beantworte zuerst die eigentliche Frage.
- Vermeide unnötige Einleitungen, Wiederholungen und überlange Erklärungen.
- Erfinde keine Fakten. Weise ehrlich auf Unsicherheiten hin.
- Verwende den Telegram-Profilnamen NICHT ständig in normalen Antworten.
- Den Profilnamen nur verwenden, wenn du jemanden persönlich begrüßt oder verabschiedest.
- Begrüße Nutzer nicht bei jeder einzelnen Nachricht erneut.
- Berücksichtige persönliche Erinnerungen nur für den jeweiligen Nutzer.
- Vermische niemals Erinnerungen verschiedener Personen in einer Gruppe.
- Behaupte nicht, etwas dauerhaft gespeichert zu haben, wenn es nicht gespeichert wurde.

FORMATIERUNG FÜR TELEGRAM:
- Nutze kurze Absätze und ausreichend Leerzeilen.
- Verwende aussagekräftige Überschriften, wenn die Antwort länger ist.
- Hebe wichtige Begriffe mit **Fettschrift** hervor.
- Verwende Aufzählungen, wenn sie die Übersicht verbessern.
- Erstelle keine Markdown-Tabellen.
- Vermeide lange, unstrukturierte Textblöcke.
- Verwende passende Emojis, wenn sie zum Inhalt passen.
- Schreibe niemals die ganze Antwort als einen einzigen Textblock.
- Gib Code nur in Codeblöcken aus, wenn Code gefragt ist.

BESONDERS BEI BILDERN:
- Beschreibe nur, was tatsächlich erkennbar ist.
- Wenn ein Bild zur Analyse geschickt wird, beschreibe es ausführlich,
  sofern der Nutzer nicht ausdrücklich eine kurze Antwort verlangt.
- Beschreibe Hauptmotiv, sichtbare Details, Umgebung, Hintergrund,
  Farben, Formen, Anordnung, Licht, Perspektive und Bildwirkung.
- Erfinde keine Details, die im Bild nicht erkennbar sind.

Wenn jemand fragt, wer dich programmiert oder erfunden hat,
antworte: "Der liebe Heiko aus dem Schwabenländle. 😊"

Sei jederzeit hilfreich, respektvoll und geduldig.
""".strip()

# Kurzzeitverlauf ist jetzt pro Chat UND Nutzer getrennt.
chat_history = defaultdict(list)

# Aktivität der Gruppe bleibt pro Chat bestehen.
group_active_until = {}

MAX_HISTORY_MESSAGES = 12
GROUP_ACTIVE_SECONDS = 180

# Nutzer werden nach 12 Stunden ohne Kontakt erneut begrüßt.
GREETING_AFTER_HOURS = 12


# ============================================================
# DATENBANK UND PERSÖNLICHES GEDÄCHTNIS
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
            # Alte Tabellen bleiben erhalten, damit bestehende Daten
            # nicht beim Update gelöscht werden.
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

            # Neue Erinnerungen sind pro Chat und Telegram-Nutzer getrennt.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_memories (
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    memory_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id, memory_key, memory_value)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_conversation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)

    except Exception:
        logger.exception("Datenbank konnte nicht initialisiert werden.")


def save_memory(chat_id, user_id, key, value):
    """Speichert eine Erinnerung für genau einen Nutzer in einem Chat."""
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO user_memories(
                    chat_id, user_id, memory_key, memory_value, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id, memory_key, memory_value)
                DO UPDATE SET updated_at=excluded.updated_at
            """, (
                str(chat_id),
                str(user_id),
                str(key)[:100],
                str(value)[:1000],
                datetime.now(timezone.utc).isoformat(),
            ))
        return True

    except Exception:
        logger.exception("Speichern der persönlichen Erinnerung fehlgeschlagen.")
        return False


def get_memories(chat_id, user_id, limit=30):
    """Lädt ausschließlich Erinnerungen des betreffenden Nutzers."""
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT memory_key, memory_value
                FROM user_memories
                WHERE chat_id=? AND user_id=?
                ORDER BY updated_at DESC
                LIMIT ?
            """, (str(chat_id), str(user_id), limit)).fetchall()

        return rows

    except Exception:
        logger.exception("Lesen der persönlichen Erinnerungen fehlgeschlagen.")
        return []


def forget_memory(chat_id, user_id, key):
    try:
        with db_connect() as conn:
            cur = conn.execute("""
                DELETE FROM user_memories
                WHERE chat_id=? AND user_id=?
                  AND (memory_key LIKE ? OR memory_value LIKE ?)
            """, (
                str(chat_id),
                str(user_id),
                f"%{key}%",
                f"%{key}%",
            ))
            return cur.rowcount

    except Exception:
        logger.exception("Löschen der persönlichen Erinnerung fehlgeschlagen.")
        return 0


def log_message(chat_id, user_id, role, content):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO user_conversation_log(
                    chat_id, user_id, role, content, created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(chat_id),
                str(user_id),
                role,
                str(content)[:4000],
                datetime.now(timezone.utc).isoformat(),
            ))

    except Exception:
        logger.exception("Persönliches Nachrichtenprotokoll konnte nicht gespeichert werden.")


def get_recent_logged_messages(chat_id, user_id, limit=MAX_HISTORY_MESSAGES):
    """Lädt den letzten Gesprächsverlauf auch nach einem Neustart."""
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT role, content
                FROM user_conversation_log
                WHERE chat_id=? AND user_id=?
                ORDER BY id DESC
                LIMIT ?
            """, (str(chat_id), str(user_id), limit)).fetchall()

        rows.reverse()
        return [
            {"role": role, "content": content}
            for role, content in rows
        ]

    except Exception:
        logger.exception("Laden des gespeicherten Gesprächsverlaufs fehlgeschlagen.")
        return []


def clear_user_history(chat_id, user_id):
    """Setzt nur den Verlauf dieses Nutzers zurück, nicht seine Erinnerungen."""
    try:
        with db_connect() as conn:
            conn.execute("""
                DELETE FROM user_conversation_log
                WHERE chat_id=? AND user_id=?
            """, (str(chat_id), str(user_id)))
        return True

    except Exception:
        logger.exception("Gesprächsverlauf konnte nicht zurückgesetzt werden.")
        return False


def get_display_name(update):
    """Liefert den Namen aus dem Telegram-Profil."""
    user = update.effective_user

    if not user:
        return "Freund"

    name = (user.full_name or user.first_name or "").strip()
    return name[:100] if name else "Freund"


def should_greet_user(chat_id, user_id):
    """Begrüßt einen Nutzer nur beim ersten Kontakt oder nach längerer Pause."""
    try:
        with db_connect() as conn:
            row = conn.execute("""
                SELECT last_seen
                FROM user_sessions
                WHERE chat_id=? AND user_id=?
            """, (str(chat_id), str(user_id))).fetchone()

        if not row:
            return True

        last_seen = datetime.fromisoformat(row[0])
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)

        return (
            datetime.now(timezone.utc) - last_seen
            >= timedelta(hours=GREETING_AFTER_HOURS)
        )

    except Exception:
        logger.exception("Nutzersitzung konnte nicht geprüft werden.")
        return False


def update_user_session(chat_id, user_id, display_name):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO user_sessions(
                    chat_id, user_id, display_name, last_seen
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET
                    display_name=excluded.display_name,
                    last_seen=excluded.last_seen
            """, (
                str(chat_id),
                str(user_id),
                str(display_name)[:100],
                datetime.now(timezone.utc).isoformat(),
            ))
    except Exception:
        logger.exception("Nutzersitzung konnte nicht gespeichert werden.")


def learn_from_text(chat_id, user_id, text):
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

            if value:
                if save_memory(chat_id, user_id, key, value):
                    return f"Alles klar, ich merke mir: {key} – {value} 😊"

    return None


# ============================================================
# MARKDOWN IN TELEGRAM-HTML UMWANDELN
# ============================================================

def markdown_to_telegram_html(text):
    """Konvertiert Markdown sicher für Telegram."""
    text = ((text or "").replace("\r\n", "\n").replace("\r", "\n"))

    lines = text.split("\n")
    converted_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if i + 1 < len(lines) and line.count("|") >= 2:
            separator = lines[i + 1].strip()

            if (
                separator.count("|") >= 2
                and re.fullmatch(r"[|\s:-]+", separator)
                and "-" in separator
            ):
                headers = [
                    cell.strip()
                    for cell in line.strip().strip("|").split("|")
                ]
                i += 2

                while i < len(lines) and lines[i].count("|") >= 2:
                    cells = [
                        cell.strip()
                        for cell in lines[i].strip().strip("|").split("|")
                    ]
                    parts = []

                    for j, cell in enumerate(cells):
                        if not cell:
                            continue
                        label = headers[j] if j < len(headers) else ""
                        parts.append(f"{label}: {cell}" if label else cell)

                    if parts:
                        converted_lines.append("• " + " — ".join(parts))
                    i += 1

                continue

        converted_lines.append(line)
        i += 1

    text = "\n".join(converted_lines)
    code_blocks = []

    def save_code_block(match):
        code = html.escape(match.group(2), quote=False)
        code_blocks.append(f"<pre>{code}</pre>")
        return f"NEXUSCODEPLACEHOLDER{len(code_blocks) - 1}END"

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```",
        save_code_block,
        text,
        flags=re.S,
    )

    text = html.escape(text, quote=False)
    text = re.sub(r"(?m)^[ \t]*(?:---+|___+|\*\*\*+)[ \t]*$", "", text)
    text = re.sub(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*#*[ \t]*$",
        r"<b>\1</b>",
        text,
    )
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^\n]+?)__", r"<u>\1</u>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?m)^[ \t]*[-*][ \t]+", "• ", text)

    for index, block in enumerate(code_blocks):
        text = text.replace(f"NEXUSCODEPLACEHOLDER{index}END", block)

    return text.strip() or "Entschuldigung, ich habe gerade keine Antwort."


async def send_long_text(message, text, parse_mode=None):
    """Teilt lange Antworten auf, ohne Leerzeilen zu verlieren."""
    text = (
        text or "Entschuldigung, ich habe gerade keine Antwort."
    ).replace("\r\n", "\n").replace("\r", "\n")

    chunks = []
    remaining = text
    max_chunk = 3500

    while len(remaining) > max_chunk:
        split_at = remaining.rfind("\n\n", 0, max_chunk)

        if split_at < 1000:
            split_at = remaining.rfind("\n", 0, max_chunk)
        if split_at < 1000:
            split_at = remaining.rfind(" ", 0, max_chunk)
        if split_at < 1000:
            split_at = max_chunk

        if remaining[split_at:split_at + 2] == "\n\n":
            split_at += 2
        elif remaining[split_at:split_at + 1] == "\n":
            split_at += 1
        elif remaining[split_at:split_at + 1] == " ":
            split_at += 1

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)

    for chunk in chunks:
        formatted = markdown_to_telegram_html(chunk)

        try:
            await message.reply_text(formatted, parse_mode="HTML")
        except Exception:
            logger.exception("Telegram-HTML fehlgeschlagen; sende Klartext.")
            plain_text = html.unescape(
                re.sub(r"</?(?:b|i|u|code|pre)>", "", formatted)
            )
            await message.reply_text(plain_text[:4000])


# ============================================================
# KI-ANTWORTEN — GEDÄCHTNIS UND VERLAUF PRO PERSON
# ============================================================

def get_ai_reply(chat_id, user_id, user_text):
    if not groq_client:
        return (
            "Meine KI ist noch nicht verbunden. "
            "Bitte prüfe GROQ_API_KEY in Render."
        )

    memories = get_memories(chat_id, user_id)
    memory_text = "\n".join(
        f"- {key}: {value}" for key, value in memories
    )

    # Verlauf dieses Nutzers im aktuellen Chat laden.
    history_key = f"{chat_id}:{user_id}"
    history = chat_history[history_key]

    if not history:
        history = get_recent_logged_messages(chat_id, user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if memory_text:
        messages.append({
            "role": "system",
            "content": (
                "Persönliche Erinnerungen ausschließlich für diesen Nutzer. "
                "Nicht mit anderen Gruppenmitgliedern vermischen:\n"
                + memory_text
            ),
        })

    messages.extend(history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_text})

    try:
        result = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=600,
        )

        answer = (
            result.choices[0].message.content
            or "Ich habe gerade keine Antwort."
        )

        history.extend([
            {"role": "user", "content": user_text[:3000]},
            {"role": "assistant", "content": answer[:3000]},
        ])

        chat_history[history_key] = history[-MAX_HISTORY_MESSAGES:]

        log_message(chat_id, user_id, "user", user_text)
        log_message(chat_id, user_id, "assistant", answer)

        return answer

    except Exception:
        logger.exception("Groq-Anfrage fehlgeschlagen.")
        return (
            "Ups, meine KI hat gerade ein Problem. "
            "Bitte prüfe GROQ_API_KEY und GROQ_MODEL in Render."
        )


# ============================================================
# BILDWÜNSCHE AUTOMATISCH ERKENNEN
# ============================================================

def extract_image_request(text, has_source_image=False):
    if not text:
        return None

    original = text.strip()
    cleaned = re.sub(r"\s+", " ", original).strip()
    low = cleaned.lower()

    edit_patterns = [
        r"\bbearbeite\b", r"\bbearbeiten\b", r"\bverändere\b",
        r"\bveraendere\b", r"\bänder(?:e|n)\b", r"\baendere\b",
        r"\bmach(?:e)?\s+(?:das|dieses|dies|mein|mein hochgeladenes)\s+bild\b",
        r"\bmach(?:e)?\s+(?:daraus|es)\b",
        r"\bgestalte\s+(?:das|dieses|mein)\s+bild\b",
        r"\bfüge\b", r"\bfuege\b",
        r"\bschreib(?:e)?\b.*\b(?:bild|foto|schrift|text|schriftzug)\b",
        r"\bschreib(?:e)?\b.*\bdazu\b",
        r"\bsetz(?:e)?\b.*\b(?:text|schrift|schriftzug)\b",
        r"\bmit\s+eleganter\s+schrift\b",
        r"\bin\s+eleganter\s+schrift\b",
        r"\bals\s+fantasy\b", r"\bals\s+comic\b", r"\bals\s+anime\b",
        r"\bals\s+gemälde\b", r"\bals\s+gemaelde\b",
        r"\bals\s+neues\s+bild\b", r"\bneu\s+gestalten\b",
        r"\bneu\s+erstellen\b", r"\bneu\s+interpretieren\b",
        r"\bverbessere\s+(?:das|dieses|mein)\s+bild\b",
    ]

    wants_edit = any(
        re.search(pattern, low, flags=re.IGNORECASE)
        for pattern in edit_patterns
    )

    if has_source_image:
        if wants_edit or re.search(r"\b(bild|foto|fotoaufnahme)\b", low):
            return "edit", cleaned or (
                "Bearbeite das hochgeladene Bild und verbessere es kreativ."
            )

    elif wants_edit and re.search(r"\b(bild|foto|schrift|schriftzug)\b", low):
        if re.search(r"\b(?:neues|neu|neuen|neue)\s+bild\b", low):
            return "create", cleaned

    create_patterns = [
        r"\b(?:erstelle|erstell|generiere|zeichne|male|erzeuge|mach|mache)\b.*\bbild\b",
        r"\bich möchte\b.*\bbild\b",
        r"\bich moechte\b.*\bbild\b",
        r"\bich will\b.*\bbild\b",
        r"\bich hätte gern\b.*\bbild\b",
        r"\bich haette gern\b.*\bbild\b",
        r"\bkannst du\b.*\bbild\b",
        r"\bmach mir\b.*\bbild\b",
        r"\bgestalte mir\b.*\bbild\b",
        r"\bzeichne mir\b", r"\bmale mir\b", r"\b/bild\b",
    ]

    if any(
        re.search(pattern, low, flags=re.IGNORECASE)
        for pattern in create_patterns
    ):
        prompt = cleaned

        remove_patterns = [
            r"^(?:nexus[,!: ]*)?",
            r"^(?:ich möchte|ich moechte|ich will)\s*,?\s*(?:dass\s+du\s+)?",
            r"^(?:kannst du|könntest du|koenntest du)\s+",
            r"^(?:bitte\s+)?",
            r"^(?:erstelle|erstell|generiere|erzeuge|zeichne|male)(?:\s+mir)?\s+",
            r"^(?:mach|mache)\s+(?:mir\s+)?",
            r"^(?:ein|eine|einen)\s+bild\s+",
            r"^(?:ein|eine|einen)\s+bild\s+(?:von|mit)\s+",
        ]

        for pattern in remove_patterns:
            prompt = re.sub(pattern, "", prompt, flags=re.IGNORECASE).strip()

        prompt = re.sub(
            r"^(?:von|mit|über|ueber)\s+",
            "",
            prompt,
            flags=re.IGNORECASE,
        ).strip(" .,:!?")

        if len(prompt) < 3:
            prompt = cleaned

        return "create", prompt

    if re.search(r"\b(?:ein|eine|einen)\s+bild\s+(?:von|mit)\s+", low):
        return "create", cleaned

    return None


# ============================================================
# BILDER FÜR AI HORDE VORBEREITEN
# ============================================================

async def telegram_photo_to_horde_base64(photo_message, context):
    if not photo_message or not photo_message.photo:
        raise ValueError("Es wurde kein verwendbares Telegram-Foto gefunden.")

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Für die Bildbearbeitung fehlt Pillow. "
            "Bitte füge 'Pillow' zu requirements.txt hinzu."
        ) from exc

    telegram_file = await context.bot.get_file(photo_message.photo[-1].file_id)
    temp_input = None
    temp_output = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            temp_input = tmp.name

        await telegram_file.download_to_drive(temp_input)

        with Image.open(temp_input) as image:
            image = image.convert("RGB")
            image.thumbnail((1536, 1536))

            with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tmp:
                temp_output = tmp.name

            image.save(temp_output, format="WEBP", quality=90, method=4)

        with open(temp_output, "rb") as image_file:
            image_bytes = image_file.read()

        if not image_bytes:
            raise RuntimeError("Das hochgeladene Bild konnte nicht gelesen werden.")

        return base64.b64encode(image_bytes).decode("ascii")

    finally:
        for path in (temp_input, temp_output):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


# ============================================================
# AI HORDE — BILD ERSTELLEN ODER BEARBEITEN
# ============================================================

def horde_headers():
    return {
        "apikey": AI_HORDE_API_KEY,
        "Client-Agent": AI_HORDE_CLIENT_AGENT,
        "Content-Type": "application/json",
    }


def horde_submit_sync(payload):
    response = requests.post(
        f"{AI_HORDE_API}/generate/async",
        headers=horde_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def horde_check_sync(request_id):
    response = requests.get(
        f"{AI_HORDE_API}/generate/check/{request_id}",
        headers=horde_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def horde_status_sync(request_id):
    response = requests.get(
        f"{AI_HORDE_API}/generate/status/{request_id}",
        headers=horde_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def horde_download_sync(image_url):
    response = requests.get(image_url, timeout=60)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()

    if not content_type.startswith("image/"):
        raise RuntimeError("AI Horde hat keine gültige Bilddatei zurückgegeben.")

    return response.content, content_type


async def generate_horde_image(message, prompt, context, source_message=None):
    if not AI_HORDE_API_KEY:
        await message.reply_text(
            "⚠️ Für die Bilderstellung fehlt AI_HORDE_API_KEY in Render."
        )
        return

    prompt = (prompt or "").strip()
    if not prompt:
        await message.reply_text("Bitte beschreibe, was ich erstellen oder bearbeiten soll.")
        return

    source_image = None
    editing = source_message is not None

    try:
        if editing:
            await message.reply_text(
                "🖼️ Ich bereite dein Bild vor und bearbeite es "
                "nach deiner Beschreibung. Das kann etwas dauern."
            )
            source_image = await telegram_photo_to_horde_base64(source_message, context)
            full_prompt = (
                "Edit the provided source image according to the following "
                "user instructions. Preserve the main subject and important "
                "composition where possible. Make only the requested changes "
                "and produce a coherent, high-quality image. User instructions: "
                + prompt
            )
        else:
            await message.reply_text(
                "🎨 Ich erstelle dein Bild. Je nach Auslastung kann es etwas dauern."
            )
            full_prompt = "Create a high-quality image based on this request: " + prompt

        payload = {
            "prompt": full_prompt,
            "params": {
                "width": AI_HORDE_IMAGE_WIDTH,
                "height": AI_HORDE_IMAGE_HEIGHT,
                "steps": 25,
                "n": 1,
                "cfg_scale": 7,
            },
            "nsfw": False,
            "censor_nsfw": True,
            "shared": False,
            "r2": True,
        }

        if editing and source_image:
            payload["source_image"] = source_image
            payload["source_processing"] = "img2img"
            payload["params"]["denoising_strength"] = 0.55

        response_data = await asyncio.to_thread(horde_submit_sync, payload)
        request_id = response_data.get("id")

        if not request_id:
            raise RuntimeError(response_data.get("message", "AI Horde hat keine Auftrags-ID geliefert."))

        logger.info("AI-Horde-Auftrag gestartet: %s; Bearbeitung=%s", request_id, editing)
        deadline = asyncio.get_running_loop().time() + AI_HORDE_TIMEOUT_SECONDS

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(5)
            status = await asyncio.to_thread(horde_check_sync, request_id)

            if status.get("faulted"):
                raise RuntimeError("AI Horde konnte den Bildauftrag nicht ausführen.")

            if status.get("done"):
                break
        else:
            raise TimeoutError("AI Horde hat innerhalb der erlaubten Wartezeit kein fertiges Bild geliefert.")

        result_data = await asyncio.to_thread(horde_status_sync, request_id)
        generations = result_data.get("generations") or []

        if not generations:
            raise RuntimeError("AI Horde meldet den Auftrag als fertig, hat aber kein Bild zurückgegeben.")

        image_url = generations[0].get("img")
        if not image_url:
            raise RuntimeError("AI Horde hat keine Bildadresse zurückgegeben.")

        image_bytes, content_type = await asyncio.to_thread(horde_download_sync, image_url)
        suffix = ".webp" if "webp" in content_type else ".png"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            result_path = tmp.name

        try:
            caption_prefix = "✅ Bearbeitetes Bild" if editing else "✅ Dein neues Bild"
            caption = f"{caption_prefix}\n\n{prompt[:850]}"
            with open(result_path, "rb") as photo:
                await message.reply_photo(photo=photo, caption=caption)
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass

        logger.info("AI-Horde-Bild erfolgreich gesendet. Auftrag=%s", request_id)

    except Exception as exc:
        logger.exception("AI-Horde-Bilderstellung/Bearbeitung fehlgeschlagen.")

        if isinstance(exc, TimeoutError):
            error_message = "⏳ AI Horde hat zu lange gebraucht. Bitte versuche es später erneut."
        elif isinstance(exc, requests.HTTPError):
            status_code = exc.response.status_code if exc.response is not None else None

            if status_code == 401:
                error_message = "⚠️ AI Horde hat den API-Schlüssel abgelehnt. Bitte prüfe AI_HORDE_API_KEY in Render."
            elif status_code == 429:
                error_message = "⚠️ AI Horde ist momentan stark ausgelastet oder es gibt zu viele Anfragen. Bitte versuche es später erneut."
            else:
                error_message = f"⚠️ AI Horde meldet HTTP {status_code}. Bitte prüfe die Render Runtime Logs."
        elif "Pillow" in str(exc):
            error_message = "⚠️ Für die Bearbeitung fehlt Pillow. Füge 'Pillow' zu requirements.txt hinzu und starte Render neu."
        else:
            error_message = (
                "⚠️ Das Bild konnte gerade nicht erstellt oder bearbeitet werden. "
                "Bitte versuche es später erneut. Weitere Details stehen in den Render Runtime Logs."
            )

        await message.reply_text(error_message)


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = get_display_name(update)
    await update.effective_message.reply_text(
        f"Hallo {name}! Ich bin NEXUS, dein KI-Assistent. 🤖😊\n\n"
        "Du kannst mir einfach schreiben, was du möchtest.\n"
        "Ich kann auch Bilder erstellen und hochgeladene Bilder bearbeiten.\n\n"
        "Beispiele:\n"
        "• Erstelle mir ein Bild von einem Schloss.\n"
        "• Schreibe „Hallo“ in eleganter Schrift auf dieses Bild.\n"
        "• Verwandle dieses Bild in eine Fantasy-Version.\n\n"
        "Befehle:\n"
        "/merken Text – etwas persönlich speichern\n"
        "/gedaechtnis – deine Erinnerungen ansehen\n"
        "/vergiss Text – eine Erinnerung löschen\n"
        "/reset – deinen Chat-Verlauf zurücksetzen\n"
        "/bild Beschreibung – neues Bild erstellen"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    history_key = f"{chat.id}:{user.id}"
    chat_history.pop(history_key, None)
    clear_user_history(chat.id, user.id)

    await update.effective_message.reply_text(
        "Dein persönlicher Chat-Verlauf wurde zurückgesetzt. 🧹\n"
        "Deine gespeicherten Erinnerungen bleiben erhalten."
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    value = " ".join(context.args).strip()

    if not chat or not user:
        return

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

    if save_memory(chat.id, user.id, key, value):
        await update.effective_message.reply_text(
            f"Persönlich gespeichert! 🧠\n{key}: {value}"
        )
    else:
        await update.effective_message.reply_text(
            "Ich konnte das leider nicht speichern. Prüfe den Datenbankpfad in Render."
        )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    memories = get_memories(chat.id, user.id)

    if not memories:
        await update.effective_message.reply_text(
            "Für dich habe ich hier noch keine persönlichen Erinnerungen gespeichert. 🧠"
        )
        return

    text = "🧠 Deine persönlichen Erinnerungen:\n" + "\n".join(
        f"• {key}: {value}" for key, value in memories
    )
    await send_long_text(update.effective_message, text)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    key = " ".join(context.args).strip()

    if not chat or not user:
        return

    if not key:
        await update.effective_message.reply_text(
            "Schreib bitte dazu, was ich vergessen soll.\nBeispiel: /vergiss Hobby"
        )
        return

    count = forget_memory(chat.id, user.id, key)

    if count:
        await update.effective_message.reply_text("Deine Erinnerung wurde gelöscht. 🧹")
    else:
        await update.effective_message.reply_text(
            "Ich habe dazu keine passende persönliche Erinnerung gefunden."
        )


# ============================================================
# /BILD — NEUES BILD ERSTELLEN
# ============================================================

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    prompt = " ".join(context.args).strip()

    if not prompt:
        await message.reply_text(
            "Bitte beschreibe das gewünschte Bild.\n"
            "Beispiel:\n"
            "/bild ein märchenhaftes Schloss im Sonnenuntergang"
        )
        return

    await generate_horde_image(
        message=message,
        prompt=prompt,
        context=context,
        source_message=None,
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
                file=(os.path.basename(file_path), audio_file.read()),
                model=WHISPER_MODEL,
                response_format="text",
                language="de",
            )

    transcription = await asyncio.to_thread(transcribe)

    if isinstance(transcription, str):
        return transcription.strip()

    return getattr(transcription, "text", "").strip()


def clean_markdown_for_speech(text):
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:---+|___+|\*\*\*+)[ \t]*$", "", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "", text)
    text = re.sub(r"(?m)^[ \t]*\d+[.)][ \t]+", "", text)
    text = text.replace("**", "").replace("*", "").replace("`", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def make_voice_file(text, path):
    try:
        import edge_tts

        speech_text = clean_markdown_for_speech(text)
        communicate = edge_tts.Communicate(speech_text[:3000], EDGE_TTS_VOICE)
        await communicate.save(path)
        return True

    except Exception:
        logger.exception("Sprachausgabe fehlgeschlagen.")
        return False


async def send_reply_in_preferred_format(message, text, voice_reply=False):
    if voice_reply:
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

    await send_long_text(message, text)


# ============================================================
# GRUPPENSTEUERUNG
# ============================================================

def is_group(chat_type):
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_addressed(text, bot_username=""):
    low = (text or "").lower().strip()
    username = (bot_username or "").lower().lstrip("@")

    if username and f"@{username}" in low:
        return True

    return bool(re.search(r"\b(nexus|ki-assistent|ki assistent|bot)\b", low))


def is_stop_phrase(text):
    low = (text or "").lower().strip()

    if re.search(r"\b(stopp|stop|halt|ruhe)\b", low):
        return True

    stop_phrases = (
        "nexus aus", "nexus stopp", "stopp nexus", "stop nexus",
        "ruhe nexus", "tschüss nexus", "tschues nexus",
        "auf wiedersehen nexus", "geh offline", "sei still nexus",
        "nexus schlaf", "nexus sei still",
    )
    return any(phrase in low for phrase in stop_phrases)


def is_farewell_phrase(text):
    low = (text or "").lower().strip()

    farewell_phrases = (
        "tschüss", "tschues", "tschüssi", "tschuess",
        "bye", "goodbye", "good bye", "ciao",
        "auf wiedersehen", "aufwiedersehen",
        "bis später", "bis spaeter", "bis dann",
        "bis bald", "gute nacht", "mach's gut", "mach’s gut",
        "macht's gut", "macht’s gut", "ich muss los",
        "ich bin dann weg", "bin dann weg", "ich gehe jetzt",
        "ich geh jetzt", "schönen abend", "schoenen abend",
        "schönes wochenende", "schoenes wochenende",
        "bis morgen", "bis zum nächsten mal",
        "bis zum naechsten mal", "ich verabschiede mich",
    )
    return any(phrase in low for phrase in farewell_phrases)


def group_should_respond(chat_id, text, bot_username=""):
    now = asyncio.get_running_loop().time()

    if is_stop_phrase(text):
        group_active_until.pop(chat_id, None)
        return False

    # Verabschiedungen werden in handle_text separat verarbeitet.
    if is_farewell_phrase(text):
        return (
            is_addressed(text, bot_username)
            or now < group_active_until.get(chat_id, 0)
        )

    addressed = is_addressed(text, bot_username)
    active_until = group_active_until.get(chat_id, 0)

    if addressed or now < active_until:
        group_active_until[chat_id] = now + GROUP_ACTIVE_SECONDS
        return True

    group_active_until.pop(chat_id, None)
    return False


def greeting_prefix(chat_id, user_id, display_name):
    """Erzeugt nur dann eine Begrüßung, wenn sie fällig ist."""
    if should_greet_user(chat_id, user_id):
        return f"Hallo {display_name}! 👋 Schön, dass du da bist.\n\n"
    return ""


def farewell_reply(display_name):
    return f"Mach's gut, {display_name}! 😊 Bis zum nächsten Mal."


# ============================================================
# TEXTNACHRICHTEN VERARBEITEN
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not message.text or not user:
        return

    text = message.text.strip()
    group = is_group(chat.type)
    display_name = get_display_name(update)
    replied_photo = None

    if message.reply_to_message and message.reply_to_message.photo:
        replied_photo = message.reply_to_message

    image_request = extract_image_request(
        text,
        has_source_image=replied_photo is not None,
    )

    if image_request:
        request_type, prompt = image_request

        if request_type == "edit" and replied_photo:
            await generate_horde_image(
                message=message,
                prompt=prompt,
                context=context,
                source_message=replied_photo,
            )
            return

        if request_type == "create":
            await generate_horde_image(
                message=message,
                prompt=prompt,
                context=context,
                source_message=None,
            )
            return

        if request_type == "edit":
            await message.reply_text(
                "🖼️ Ich kann das Bild bearbeiten. Bitte lade das Bild "
                "zusammen mit deiner Anweisung hoch oder antworte direkt "
                "auf ein Bild mit deiner Änderungsbeschreibung."
            )
            return

    # In Gruppen antwortet NEXUS nur, wenn er angesprochen wurde
    # oder die Unterhaltung mit ihm noch aktiv ist.
    if group and not group_should_respond(chat.id, text, context.bot.username or ""):
        return

    if is_farewell_phrase(text):
        answer = farewell_reply(display_name)
        update_user_session(chat.id, user.id, display_name)
        group_active_until.pop(chat.id, None)
        await message.reply_text(answer)
        return

    learned = learn_from_text(chat.id, user.id, text)

    if learned:
        prefix = greeting_prefix(chat.id, user.id, display_name)
        update_user_session(chat.id, user.id, display_name)
        await message.reply_text(prefix + learned)
        return

    await message.chat.send_action("typing")
    reply = await asyncio.to_thread(get_ai_reply, chat.id, user.id, text)

    prefix = greeting_prefix(chat.id, user.id, display_name)
    update_user_session(chat.id, user.id, display_name)

    await send_reply_in_preferred_format(
        message,
        prefix + reply,
        voice_reply=False,
    )


# ============================================================
# SPRACHNACHRICHTEN VERARBEITEN
# ============================================================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not message.voice or not user:
        return

    group = is_group(chat.type)
    display_name = get_display_name(update)

    if not groq_client:
        if not group:
            await message.reply_text(
                "Meine Spracherkennung ist noch nicht verbunden. Bitte prüfe GROQ_API_KEY."
            )
        return

    if not group:
        await message.reply_text("Ich höre zu … 🎧")

    file_path = None

    try:
        tg_file = await context.bot.get_file(message.voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            file_path = tmp.name

        await tg_file.download_to_drive(file_path)
        spoken_text = await transcribe_voice(file_path)

        if not spoken_text:
            if not group:
                await message.reply_text("Ich konnte die Sprachnachricht leider nicht verstehen.")
            return

        image_request = extract_image_request(spoken_text)

        if image_request:
            request_type, prompt = image_request

            if request_type == "create":
                await generate_horde_image(
                    message=message,
                    prompt=prompt,
                    context=context,
                    source_message=None,
                )
                return

            if request_type == "edit":
                await message.reply_text(
                    "🖼️ Bitte lade das Bild hoch und schreibe deine Änderungswünsche dazu."
                )
                return

        if group and not group_should_respond(
            chat.id, spoken_text, context.bot.username or ""
        ):
            return

        if is_farewell_phrase(spoken_text):
            update_user_session(chat.id, user.id, display_name)
            group_active_until.pop(chat.id, None)
            await send_reply_in_preferred_format(
                message,
                farewell_reply(display_name),
                voice_reply=True,
            )
            return

        learned = learn_from_text(chat.id, user.id, spoken_text)

        if learned:
            prefix = greeting_prefix(chat.id, user.id, display_name)
            update_user_session(chat.id, user.id, display_name)
            await send_reply_in_preferred_format(
                message, prefix + learned, voice_reply=True
            )
            return

        reply = await asyncio.to_thread(
            get_ai_reply, chat.id, user.id, spoken_text
        )

        prefix = greeting_prefix(chat.id, user.id, display_name)
        update_user_session(chat.id, user.id, display_name)

        await send_reply_in_preferred_format(
            message, prefix + reply, voice_reply=True
        )

    except Exception:
        logger.exception("Verarbeitung der Sprachnachricht fehlgeschlagen.")

        if not group:
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
# GEMINI-FOTOANALYSE MIT RETRIES UND ERSATZMODELL
# ============================================================

def is_retryable_gemini_error(exc):
    error_text = str(exc).lower()
    return any(
        marker in error_text
        for marker in (
            "503", "unavailable", "high demand", "429",
            "resource_exhausted", "500", "internal server error",
            "502", "504", "deadline_exceeded", "timeout", "timed out",
        )
    )


def count_words(text):
    return len(re.findall(r"\b[\w'-]+\b", text or "", flags=re.UNICODE))


async def analyze_photo_with_gemini(client, image_prompt, image_part):
    from google.genai import types

    models = []
    for model in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
        if model and model not in models:
            models.append(model)

    if not models:
        raise RuntimeError("Es wurde kein Gemini-Modell konfiguriert.")

    last_error = None

    for model_index, model in enumerate(models):
        for attempt in range(GEMINI_MAX_ATTEMPTS_PER_MODEL):
            try:
                logger.info(
                    "Fotoanalyse: Modell=%s, Versuch=%s/%s",
                    model, attempt + 1, GEMINI_MAX_ATTEMPTS_PER_MODEL,
                )

                request = asyncio.to_thread(
                    lambda selected_model=model: client.models.generate_content(
                        model=selected_model,
                        contents=[image_prompt, image_part],
                        config=types.GenerateContentConfig(
                            temperature=0.35,
                            max_output_tokens=GEMINI_PHOTO_MAX_OUTPUT_TOKENS,
                        ),
                    )
                )

                result = await asyncio.wait_for(
                    request, timeout=GEMINI_ATTEMPT_TIMEOUT
                )
                answer = getattr(result, "text", None)
                answer = answer.strip() if answer else ""

                if not answer:
                    raise RuntimeError(
                        f"Gemini-Modell {model} hat keinen Text geliefert."
                    )

                word_count = count_words(answer)

                if (
                    word_count < GEMINI_PHOTO_MIN_WORDS
                    and attempt + 1 < GEMINI_MAX_ATTEMPTS_PER_MODEL
                ):
                    logger.warning(
                        "Fotoanalyse zu kurz (%s Wörter). Fordere eine ausführlichere Beschreibung an.",
                        word_count,
                    )

                    expanded_prompt = (
                        image_prompt
                        + "\n\nWICHTIG: Die Antwort war zu kurz. Erstelle eine "
                        "vollständige, deutlich ausführlichere Beschreibung. "
                        "Verwende mehrere Überschriften, Absätze und 8 bis 12 "
                        "konkrete Stichpunkte, soweit das Bild genug Details "
                        "enthält. Beschreibe Hauptmotiv, Position, Merkmale, "
                        "Gegenstände, Farben, Formen, Hintergrund, Umgebung, "
                        "Beleuchtung, Schatten, Perspektive, Bildaufbau und "
                        "Bildwirkung. Ziel sind ungefähr 300 Wörter. Erfinde "
                        "keine Details."
                    )

                    expanded_request = asyncio.to_thread(
                        lambda selected_model=model: client.models.generate_content(
                            model=selected_model,
                            contents=[expanded_prompt, image_part],
                            config=types.GenerateContentConfig(
                                temperature=0.3,
                                max_output_tokens=GEMINI_PHOTO_MAX_OUTPUT_TOKENS,
                            ),
                        )
                    )

                    expanded_result = await asyncio.wait_for(
                        expanded_request, timeout=GEMINI_ATTEMPT_TIMEOUT
                    )
                    expanded_answer = getattr(expanded_result, "text", None)
                    expanded_answer = (
                        expanded_answer.strip() if expanded_answer else ""
                    )

                    if expanded_answer and count_words(expanded_answer) > word_count:
                        answer = expanded_answer
                        word_count = count_words(answer)

                logger.info(
                    "Fotoanalyse erfolgreich mit Modell %s; Antwortlänge=%s Wörter",
                    model, word_count,
                )
                return answer

            except Exception as exc:
                last_error = exc
                retryable = is_retryable_gemini_error(exc)

                logger.warning(
                    "Fotoanalysefehler: Modell=%s, Versuch=%s, vorübergehend=%s, Fehler=%s",
                    model, attempt + 1, retryable, exc,
                )

                if not retryable:
                    break

                if attempt + 1 < GEMINI_MAX_ATTEMPTS_PER_MODEL:
                    wait_seconds = 2 ** (attempt + 1)
                    logger.info("Wiederhole Gemini-Anfrage nach %s Sekunden.", wait_seconds)
                    await asyncio.sleep(wait_seconds)

        if model_index + 1 < len(models):
            logger.warning(
                "Wechsle von Gemini-Modell %s zu Ersatzmodell %s",
                model, models[model_index + 1],
            )

    if last_error:
        raise last_error

    raise RuntimeError("Alle konfigurierten Gemini-Modelle sind fehlgeschlagen.")


# ============================================================
# FOTOS ANALYSIEREN ODER BEARBEITEN
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.photo:
        return

    original_caption = (message.caption or "").strip()
    group = is_group(chat.type)

    image_request = (
        extract_image_request(original_caption, has_source_image=True)
        if original_caption
        else None
    )

    if image_request:
        request_type, prompt = image_request

        if request_type in ("edit", "create"):
            await generate_horde_image(
                message=message,
                prompt=prompt,
                context=context,
                source_message=message,
            )
            return

    if group and not group_should_respond(
        chat.id, original_caption, context.bot.username or ""
    ):
        return

    if not GEMINI_API_KEY:
        await message.reply_text("Für die Fotoanalyse fehlt GEMINI_API_KEY in Render.")
        return

    caption = original_caption or "Beschreibe dieses Bild ausführlich auf Deutsch."
    path = None
    client = None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        photo_file = await context.bot.get_file(message.photo[-1].file_id)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            path = tmp.name

        await photo_file.download_to_drive(path)

        with open(path, "rb") as image_file:
            image_bytes = image_file.read()

        if not image_bytes:
            raise RuntimeError("Die heruntergeladene Bilddatei ist leer.")

        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

        logger.info(
            "Fotoanalyse gestartet; Hauptmodell=%s, Ersatzmodell=%s, Chat=%s",
            GEMINI_MODEL, GEMINI_FALLBACK_MODEL or "(nicht konfiguriert)", chat.id,
        )

        photo_prompt = f"""
Du bist ein sorgfältiger Bildanalyst. Untersuche das angehängte Bild
genau und beschreibe es auf Deutsch ausführlich, konkret und gut lesbar.

AUFGABE DES NUTZERS:
{caption}

ZIEL:
Schreibe normalerweise ungefähr 250 bis 450 Wörter. Eine Antwort aus nur
einem Satz ist ausdrücklich nicht ausreichend, wenn das Bild mehr Details
zeigt. Nutze mehrere Absätze und die verfügbaren Ausgabetoken. Wenn das Bild
tatsächlich nur wenige Informationen enthält, darf die Beschreibung kürzer
sein. Erfinde keine Details.

GLIEDERUNG:

1. **Überschrift**
   Beginne mit einer passenden Überschrift und einem passenden Emoji.

2. **Gesamteindruck**
   Beschreibe in 3 bis 5 vollständigen Sätzen, was hauptsächlich zu sehen ist.

3. **Wichtige Bilddetails**
   Nenne 8 bis 12 konkrete Stichpunkte, sofern ausreichend Details erkennbar sind.
   Berücksichtige Personen oder Tiere, sichtbare Merkmale, Kleidung, Körperhaltung,
   Gegenstände, Symbole, lesbare Texte, Farben, Formen, Materialien und Positionen.

4. **Umgebung und Hintergrund**
   Beschreibe Hintergrund sowie erkennbare Details im Vorder- und Hintergrund.

5. **Licht, Farben und Perspektive**
   Beschreibe Beleuchtung, Schatten, Farbwirkung, Perspektive und Bildaufbau.

6. **Stimmung und Bildwirkung**
   Erkläre den möglichen Eindruck. Trenne Eindrücke von erkennbaren Tatsachen.

7. **Besonderheiten**
   Erwähne zusätzliche Details, die leicht übersehen werden könnten.

8. **Bei Spiel- oder Programm-Screenshots**
   Erkläre sichtbare Figuren, Symbole, Statusanzeigen, Menüs, Texte und Ziele.

WICHTIGE REGELN:
- Schreibe mehrere Absätze und nicht nur einen Satz.
- Verwende konkrete Beobachtungen statt allgemeiner Floskeln.
- Vermeide Wiederholungen.
- Verwende Telegram-tauglichen Markdown-Fettdruck mit **Text**.
- Nutze Emojis sparsam und passend.
- Erfinde keine Namen, Orte, Ursachen oder Beziehungen.
- Behaupte nicht, eine Person identifiziert zu haben.
- Wenn ein Detail unklar ist, sage ausdrücklich, dass es nicht eindeutig erkennbar ist.
- Beantworte konkrete Fragen des Nutzers direkt.
- Liefere nur die fertige Bildbeschreibung, keine Erklärung deiner Vorgehensweise.
"""

        answer = await asyncio.wait_for(
            analyze_photo_with_gemini(client, photo_prompt, image_part),
            timeout=GEMINI_ANALYSIS_TIMEOUT,
        )

        if not answer:
            answer = "⚠️ Ich konnte das Bild nicht zuverlässig beschreiben. Bitte versuche es erneut."

        await send_long_text(message, answer)

        logger.info(
            "Fotoanalyse erfolgreich abgeschlossen; Chat=%s; Wörter=%s",
            chat.id, count_words(answer),
        )

    except Exception as exc:
        logger.exception("Fotoanalyse fehlgeschlagen.")
        error_text = str(exc).lower()

        if "api key" in error_text or "api_key" in error_text or "unauthorized" in error_text:
            user_message = "Die Gemini-Anmeldung ist fehlgeschlagen. Bitte prüfe GEMINI_API_KEY in Render."
        elif "404" in error_text or "not found" in error_text:
            user_message = "Das Gemini-Modell wurde nicht gefunden oder ist für deinen Schlüssel nicht verfügbar. Bitte prüfe GEMINI_MODEL und GEMINI_FALLBACK_MODEL in Render."
        elif "403" in error_text or "permission" in error_text or "forbidden" in error_text:
            user_message = "Gemini hat den Zugriff verweigert. Bitte prüfe die API-Berechtigungen und den Projektzugriff."
        elif "429" in error_text or "quota" in error_text or "rate limit" in error_text or "resource_exhausted" in error_text:
            user_message = "⚠️ Das Gemini-Limit oder die Anfragengrenze wurde erreicht. Bitte warte etwas und versuche es erneut."
        elif "503" in error_text or "unavailable" in error_text or "high demand" in error_text:
            user_message = "⚠️ Gemini ist momentan stark ausgelastet. NEXUS hat Wiederholungsversuche und das konfigurierte Ersatzmodell ausprobiert. Bitte versuche es später erneut."
        elif isinstance(exc, asyncio.TimeoutError) or "timeout" in error_text or "timed out" in error_text:
            user_message = "⏳ Die Fotoanalyse hat zu lange gedauert. Bitte versuche es erneut."
        else:
            user_message = "Die Fotoanalyse ist fehlgeschlagen. Weitere Details stehen in den Render Runtime Logs."

        try:
            await message.reply_text(user_message)
        except Exception:
            logger.exception("Fehlermeldung zur Fotoanalyse konnte nicht gesendet werden.")

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

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
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
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("merken", remember_command))
    application.add_handler(CommandHandler("gedaechtnis", memories_command))
    application.add_handler(CommandHandler("vergiss", forget_command))
    application.add_handler(CommandHandler("bild", image_command))

    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
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
        raise RuntimeError("Der Telegram-Updater konnte nicht erstellt werden.")

    initialized = False
    started = False
    polling_started = False

    try:
        logger.info("Initialisiere Telegram-Anwendung …")
        await application.initialize()
        initialized = True

        await application.start()
        started = True

        await updater.start_polling(drop_pending_updates=False)
        polling_started = True

        logger.info("NEXUS Telegram-Bot läuft und empfängt Nachrichten.")
        await asyncio.Event().wait()

    finally:
        logger.info("Telegram-Bot wird heruntergefahren …")

        if polling_started:
            try:
                await updater.stop()
            except Exception:
                logger.exception("Fehler beim Stoppen des Telegram-Updaters.")

        if started:
            try:
                await application.stop()
            except Exception:
                logger.exception("Fehler beim Stoppen der Telegram-Anwendung.")

        if initialized:
            try:
                await application.shutdown()
            except Exception:
                logger.exception("Fehler beim Herunterfahren der Telegram-Anwendung.")


def main():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error("Bot startet nicht: TELEGRAM_TOKEN fehlt.")
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
            logger.info("Telegram-Bot-Thread läuft bereits.")
            return

        if not TELEGRAM_TOKEN:
            logger.warning("Telegram-Bot bleibt aus, da TELEGRAM_TOKEN fehlt.")
            return

        def runner():
            try:
                main()
            except Exception:
                logger.exception("Telegram-Bot konnte nicht gestartet werden.")

        _bot_thread = threading.Thread(
            target=runner,
            name="nexus-telegram-bot",
            daemon=True,
        )
        _bot_thread.start()
        logger.info("Telegram-Bot-Thread wurde gestartet.")


# ============================================================
# ANWENDUNG INITIALISIEREN
# ============================================================

init_db()
start_bot_thread()
