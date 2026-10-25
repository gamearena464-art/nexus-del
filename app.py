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

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """
Du bist NEXUS, ein professioneller, freundlicher und intelligenter deutscher KI-Assistent.
WICHTIG ZUR NAMENSANSPRACHE:
- NEXUS ist dein eigener Bot-Name, niemals der Name des Nutzers.
- Verwechsle den Namen NEXUS niemals mit dem Namen einer schreibenden Person.
- Wenn du eine Person namentlich ansprichst, verwende ausschließlich den vom Programm übermittelten Telegram-Profilnamen dieser Person.
- Erfinde keinen Namen und nenne den Nutzer nicht „Nexus“, nur weil du selbst NEXUS heißt.
- In normalen Antworten musst du den Namen nicht künstlich ständig wiederholen.


SPRACHE UND STIL:
- Antworte grundsätzlich auf Deutsch, außer der Nutzer wünscht eine andere Sprache.
- Schreibe natürlich, verständlich, freundlich und professionell.
- Beantworte zuerst die eigentliche Frage.
- Vermeide unnötige Einleitungen, Wiederholungen und überlange Erklärungen.
- Passe die Länge an die Frage an: einfache Frage = kurze Antwort, komplexe Frage = ausführlichere Erklärung.
- Erfinde keine Fakten. Weise ehrlich auf Unsicherheiten hin.

FORMATIERUNG FÜR TELEGRAM:
- Nutze kurze Absätze und ausreichend Leerzeilen.
- Verwende aussagekräftige Überschriften, wenn die Antwort länger ist.
- Hebe wichtige Begriffe mit **Fettschrift** hervor.
- Verwende Aufzählungen, wenn sie die Übersicht verbessern.
- Erstelle keine Markdown-Tabellen.
- Vermeide lange, unstrukturierte Textblöcke.
- Verwende sichtbar und regelmäßig passende Emojis: bei längeren Antworten in der Überschrift und bei den meisten Abschnittsüberschriften oder Stichpunkten.
- Nutze meist etwa 1–2 Emojis pro Abschnitt, sofern es zum Thema passt. Nicht wahllos dieselben Smileys wiederholen.
- Halte die Formatierung luftig: Zwischen Überschrift, Absatz und jedem neuen Abschnitt steht eine echte Leerzeile.
- Schreibe niemals die ganze Antwort als einen einzigen Textblock.
- Übertreibe nicht mit Fettschrift; nutze sie für Überschriften und wichtige Begriffe.
- Gib Code nur in Codeblöcken aus, wenn Code gefragt ist.

PASSENDE EMOJIS:
Wähle Emojis passend zum Inhalt und Kontext. Beispiele: Auto 🚗, Rad 🛞 oder 🚲, Sonne ☀️, Computer 💻, Handy 📱, Werkzeug 🛠️, Schaufel 🪏, Pflanzen 🌱, Gemüse 🥦, Essen 🍽️, Kochen 🍳, Haus 🏠, Geld 💶, Zeit ⏰, Idee 💡, Warnung ⚠️, Erfolg ✅, Frage ❓. Die Beispiele sind keine starre Liste. Wenn kein Emoji sinnvoll ist, lasse es weg.

BEI ERKLÄRUNGEN:
- Erkläre schwierige Begriffe in einfachen Worten.
- Gliedere längere Antworten in sinnvolle Abschnitte.
- Bei technischen Themen nenne bei Bedarf das Bauteil, seine Aufgabe und ein hilfreiches Beispiel.
- Beende Antworten nicht automatisch mit einer Zusammenfassung, wenn diese nichts Neues beiträgt.

BESONDERS BEI BILDERN:
- Beschreibe nur, was tatsächlich erkennbar ist.
- Beginne mit einer kurzen Aussage zum Hauptmotiv.
- Nenne anschließend die wichtigsten Details als Stichpunkte.
- Verwende passende Emojis für erkannte Gegenstände und Themen.
- Erfinde keine Details, die auf dem Bild nicht erkennbar sind.
- Bei Spiel-Screenshots erkläre kurz das Spielziel, wichtige Elemente und gegebenenfalls sinnvolle nächste Schritte.
- Halte normale Bildbeschreibungen kurz, außer der Nutzer verlangt ausdrücklich Details.

Wenn jemand fragt, wer dich programmiert oder erfunden hat, antworte: "Der liebe Heiko aus dem Schwabenländle. 😊"

Sei jederzeit hilfreich, respektvoll und geduldig.
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
                INSERT INTO memories(
                    chat_id, memory_key, memory_value, updated_at
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
                INSERT INTO conversation_log(
                    chat_id, role, content, created_at
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
            if value:
                save_memory(chat_id, key, value)
                return f"Alles klar, ich merke mir: {key} – {value} 😊"

    return None


# ============================================================
# MARKDOWN IN TELEGRAM-HTML UMWANDELN
# ============================================================

def markdown_to_telegram_html(text):
    """Konvertiert Markdown sicher für Telegram und erhält Leerzeilen."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    placeholders = {}

    def keep(value):
        key = f"ZXQPLACEHOLDER{len(placeholders)}QXZ"
        placeholders[key] = value
        return key

    # Codeblöcke schützen
    def protect_codeblock(match):
        code = html.escape(match.group(2))
        language = match.group(1) or ""
        if language:
            return keep(f"<pre><code class=\"language-{html.escape(language)}\">{code}</code></pre>")
        return keep(f"<pre>{code}</pre>")

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)\n(.*?)```",
        protect_codeblock,
        text,
        flags=re.DOTALL,
    )

    # Inline-Code schützen
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"),
        text,
    )

    # Links schützen
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f'{html.escape(m.group(1))}</a>'
        ),
        text,
    )

    # Restlichen Text HTML-sicher machen
    text = html.escape(text)

    # Markdown-Überschriften
    text = re.sub(
        r"(?m)^(#{1,6})\s+(.+)$",
        lambda m: f"<b>{m.group(2)}</b>",
        text,
    )

    # Fett
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)

    # Kursiv
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", text)

    # Durchgestrichen
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # Platzhalter wieder einsetzen
    for key, value in placeholders.items():
        text = text.replace(key, value)

    return text


async def send_long_text(message, text, parse_mode="HTML"):
    """Sendet lange Antworten in Telegram in mehreren sicheren Teilen."""
    text = (text or "").strip()
    if not text:
        return

    max_length = 4000
    chunks = []

    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = text.rfind(" ", 0, max_length)
        if split_at < max_length // 2:
            split_at = max_length

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    for chunk in chunks:
        try:
            if parse_mode == "HTML":
                await message.reply_text(chunk, parse_mode="HTML")
            else:
                await message.reply_text(chunk)
        except Exception:
            logger.exception("Antwort konnte nicht mit Formatierung gesendet werden.")
            try:
                await message.reply_text(
                    re.sub(r"<[^>]+>", "", chunk),
                    parse_mode=None,
                )
            except Exception:
                logger.exception("Auch die unformatierte Antwort konnte nicht gesendet werden.")


# ============================================================
# TELEGRAM-NAMEN UND GRUPPENLOGIK
# ============================================================

def get_display_name(update: Update) -> str:
    """Liefert den Telegram-Profilnamen der Person, die geschrieben hat."""
    user = update.effective_user
    if not user:
        return "Hallo"

    first_name = (user.first_name or "").strip()
    last_name = (user.last_name or "").strip()
    full_name = f"{first_name} {last_name}".strip()

    if full_name:
        return full_name

    username = (user.username or "").strip()
    if username:
        return f"@{username}"

    return "du"


def is_group(chat_type) -> bool:
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_greeting(text: str) -> bool:
    text = (text or "").strip().lower()
    return bool(
        re.match(
            r"^(hallo|hi|hey|servus|moin|guten morgen|guten tag|guten abend)\b",
            text,
        )
    )


def is_farewell(text: str) -> bool:
    text = (text or "").strip().lower()
    return bool(
        re.match(
            r"^(tschüss|tschues|ciao|bye|auf wiedersehen|bis dann|bis später|gute nacht)\b",
            text,
        )
    )


def is_addressed(text: str, bot_username: str) -> bool:
    """Prüft, ob eine Nachricht NEXUS direkt anspricht."""
    text = (text or "").lower().strip()
    username = (bot_username or "").lower().strip()

    if username and f"@{username}" in text:
        return True

    return bool(
        re.search(
            r"\b(nexus|hey nexus|hallo nexus|hi nexus)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def activate_group(chat_id):
    group_active_until[chat_id] = (
        asyncio.get_running_loop().time() + GROUP_ACTIVE_SECONDS
    )


def is_group_active(chat_id):
    try:
        return (
            asyncio.get_running_loop().time()
            < group_active_until.get(chat_id, 0)
        )
    except RuntimeError:
        return False


# ============================================================
# KI-ANTWORTEN
# ============================================================

def get_memory_context(chat_id):
    memories = get_memories(chat_id)
    if not memories:
        return ""

    lines = ["Bekannte Informationen über diese Person:"]
    for key, value in memories:
        lines.append(f"- {key}: {value}")

    return "\n".join(lines)


def build_messages(chat_id, user_text, display_name):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    memory_context = get_memory_context(chat_id)
    if memory_context:
        messages.append({
            "role": "system",
            "content": memory_context,
        })

    messages.append({
        "role": "system",
        "content": (
            f"Der Telegram-Profilname der Person, die gerade schreibt, ist "
            f"„{display_name}“. Wenn eine direkte persönliche Ansprache "
            f"passend ist, verwende genau diesen Namen. Nenne die Person "
            f"niemals „NEXUS“, denn NEXUS ist der Name des Bots."
        ),
    })

    history = chat_history[str(chat_id)][-MAX_HISTORY_MESSAGES:]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    return messages


async def generate_ai_response(chat_id, user_text, display_name):
    if not groq_client:
        return (
            "⚠️ Der KI-Dienst ist noch nicht eingerichtet. "
            "Bitte prüfe den GROQ_API_KEY in den Umgebungsvariablen."
        )

    messages = build_messages(chat_id, user_text, display_name)

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=1200,
                )
            ),
            timeout=45,
        )

        answer = response.choices[0].message.content
        if not answer:
            answer = "Entschuldigung, ich konnte gerade keine Antwort erstellen."

        history = chat_history[str(chat_id)]
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": answer})

        if len(history) > MAX_HISTORY_MESSAGES:
            del history[:-MAX_HISTORY_MESSAGES]

        log_message(chat_id, "user", user_text)
        log_message(chat_id, "assistant", answer)

        return answer

    except asyncio.TimeoutError:
        logger.warning("KI-Anfrage hat das Zeitlimit überschritten.")
        return "⏳ Das dauert gerade zu lange. Bitte versuche es gleich noch einmal."

    except Exception:
        logger.exception("Fehler beim Erzeugen der KI-Antwort.")
        return (
            "⚠️ Beim Erstellen meiner Antwort ist ein Fehler aufgetreten. "
            "Bitte versuche es gleich noch einmal."
        )


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    display_name = get_display_name(update)
    greeting = (
        f"Hallo {display_name}, schön, dass du da bist! 😊\n\n"
        "Ich bin **NEXUS**, dein KI-Assistent.\n\n"
        "Du kannst mir Fragen stellen, mit mir schreiben oder mir ein Bild "
        "schicken, das ich analysieren soll.\n\n"
        "Mit /gedaechtnis kannst du gespeicherte Erinnerungen ansehen.\n"
        "Mit /merken kannst du mir etwas zum Merken schreiben.\n"
        "Mit /vergiss kannst du gespeicherte Informationen löschen.\n"
        "Mit /reset setzt du den Gesprächsverlauf zurück."
    )

    await send_long_text(
        update.effective_message,
        markdown_to_telegram_html(greeting),
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return

    chat_id = str(update.effective_chat.id)
    chat_history[chat_id] = []

    display_name = get_display_name(update)
    await update.effective_message.reply_text(
        f"Alles klar, {display_name}. Unser Gesprächsverlauf wurde zurückgesetzt. 🔄"
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return

    display_name = get_display_name(update)
    text = " ".join(context.args).strip()

    if not text:
        await update.effective_message.reply_text(
            f"Was soll ich mir merken, {display_name}? "
            "Schreibe zum Beispiel: /merken Ich trinke gerne Kaffee."
        )
        return

    save_memory(update.effective_chat.id, "Notiz", text)
    await update.effective_message.reply_text(
        f"Alles klar, {display_name}, ich merke mir: {text} 😊"
    )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return

    display_name = get_display_name(update)
    memories = get_memories(update.effective_chat.id)

    if not memories:
        await update.effective_message.reply_text(
            f"Ich habe für diesen Chat noch nichts gespeichert, {display_name}. 🧠"
        )
        return

    lines = [f"🧠 Das habe ich mir für diesen Chat gemerkt, {display_name}:\n"]
    for key, value in memories:
        lines.append(f"• **{key}:** {value}")

    await send_long_text(
        update.effective_message,
        markdown_to_telegram_html("\n".join(lines)),
    )


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return

    display_name = get_display_name(update)
    key = " ".join(context.args).strip()

    if not key:
        await update.effective_message.reply_text(
            f"Welche Information soll ich vergessen, {display_name}? "
            "Verwende zum Beispiel: /vergiss Hobby"
        )
        return

    deleted = forget_memory(update.effective_chat.id, key)

    if deleted:
        await update.effective_message.reply_text(
            f"Alles klar, {display_name}. Ich habe {deleted} gespeicherte "
            f"Information(en) zu „{key}“ gelöscht. 🗑️"
        )
    else:
        await update.effective_message.reply_text(
            f"Ich habe keine gespeicherte Information zu „{key}“ gefunden, "
            f"{display_name}."
        )


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    display_name = get_display_name(update)
    await update.effective_message.reply_text(
        f"Schicke mir einfach ein Bild, {display_name}, dann analysiere ich es. 🖼️"
    )


# ============================================================
# NACHRICHTEN VERARBEITEN
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.text:
        return

    user_text = message.text.strip()
    display_name = get_display_name(update)
    bot_username = context.bot.username or ""

    if is_group(chat.type):
        addressed = is_addressed(user_text, bot_username)
        active = is_group_active(chat.id)

        if addressed:
            activate_group(chat.id)
        elif not active:
            return

    if is_greeting(user_text):
        await message.reply_text(
            f"Hallo {display_name}, schön, dass du da bist! 😊"
        )
        return

    if is_farewell(user_text):
        await message.reply_text(
            f"Tschüss {display_name}, bis bald! 👋"
        )
        return

    learned = learn_from_text(chat.id, user_text)
    if learned:
        await message.reply_text(f"{display_name}, {learned}")
        return

    await context.bot.send_chat_action(
        chat_id=chat.id,
        action="typing",
    )

    answer = await generate_ai_response(
        chat_id=chat.id,
        user_text=user_text,
        display_name=display_name,
    )

    await send_long_text(
        message,
        markdown_to_telegram_html(answer),
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.voice:
        return

    display_name = get_display_name(update)

    if is_group(chat.type):
        if not is_group_active(chat.id):
            await message.reply_text(
                f"Sprich mich zuerst mit „NEXUS“ an, {display_name}, "
                "damit ich in dieser Gruppe aktiv werde."
            )
            return

    if not GROQ_API_KEY:
        await message.reply_text(
            "⚠️ Die Spracherkennung ist nicht eingerichtet. "
            "Bitte prüfe GROQ_API_KEY."
        )
        return

    path = None

    try:
        await context.bot.send_chat_action(
            chat_id=chat.id,
            action="typing",
        )

        voice_file = await context.bot.get_file(message.voice.file_id)

        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False,
        ) as tmp:
            path = tmp.name

        await voice_file.download_to_drive(path)

        def transcribe():
            with open(path, "rb") as audio_file:
                return groq_client.audio.transcriptions.create(
                    file=audio_file,
                    model=WHISPER_MODEL,
                    response_format="text",
                )

        transcription = await asyncio.wait_for(
            asyncio.to_thread(transcribe),
            timeout=60,
        )

        user_text = str(transcription).strip()
        if not user_text:
            await message.reply_text(
                f"Ich konnte deine Sprachnachricht leider nicht verstehen, "
                f"{display_name}. Bitte versuche es erneut."
            )
            return

        await message.reply_text(
            f"🎤 **Erkannt, {display_name}:**\n{user_text}",
            parse_mode="Markdown",
        )

        learned = learn_from_text(chat.id, user_text)
        if learned:
            await message.reply_text(f"{display_name}, {learned}")
            return

        answer = await generate_ai_response(
            chat_id=chat.id,
            user_text=user_text,
            display_name=display_name,
        )

        await send_long_text(
            message,
            markdown_to_telegram_html(answer),
        )

    except asyncio.TimeoutError:
        await message.reply_text(
            f"Die Verarbeitung deiner Sprachnachricht dauert zu lange, "
            f"{display_name}. Bitte versuche es erneut."
        )
    except Exception:
        logger.exception("Verarbeitung der Sprachnachricht fehlgeschlagen.")
        await message.reply_text(
            f"Beim Verarbeiten deiner Sprachnachricht ist ein Fehler aufgetreten, "
            f"{display_name}. Bitte versuche es später erneut."
        )
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.photo:
        return

    display_name = get_display_name(update)

    if not GEMINI_API_KEY:
        await message.reply_text(
            f"Die Bildanalyse ist noch nicht eingerichtet, {display_name}. "
            "Bitte prüfe GEMINI_API_KEY in Render."
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
            and not is_addressed(caption, context.bot.username or "")
        ):
            return

    path = None
    client = None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)

        photo_file = await context.bot.get_file(message.photo[-1].file_id)

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

        photo_prompt = f"""
Analysiere das angehängte Bild sorgfältig und antworte auf Deutsch.

Aufgabe des Nutzers:
{caption}

Gib eine ausführliche, hilfreiche Beschreibung mit ungefähr 180 bis 280 Wörtern,
sofern das Bild genügend erkennbare Informationen enthält. Schreibe nicht nur
 einen einzelnen Satz. Bleibe trotzdem konkret und wiederhole dich nicht.

GLIEDERUNG:
- Beginne mit einer passenden Überschrift und einem thematisch passenden Emoji.
- **Gesamtbild:** Erkläre in 2–4 Sätzen, was hauptsächlich zu sehen ist und was passiert.
- **Wichtige Details:** Nenne mehrere konkrete Details zu sichtbaren Personen,
  Tieren, Gegenständen, Farben, Formen, Kleidung oder anderen Merkmalen.
- **Umgebung und Hintergrund:** Beschreibe den Ort, den Hintergrund,
  die Anordnung und auffällige Elemente, sofern erkennbar.
- **Stimmung und Bildwirkung:** Beschreibe Beleuchtung, Perspektive und Stimmung,
  aber kennzeichne Vermutungen klar als Vermutungen.
- **Besonderheiten:** Erwähne weitere auffällige Details, die beim Verständnis helfen.
- Bei einem Spiel-Screenshot: Erkläre sichtbare Spielinformationen, Symbole,
  Ziele oder Statusanzeigen und schlage bei ausreichenden Informationen
  einen sinnvollen nächsten Schritt vor.

WICHTIGE REGELN:
- Nutze mehrere kurze Absätze und bei Bedarf 4–8 aussagekräftige Stichpunkte.
- Verwende passende Emojis sparsam und Telegram-tauglichen Markdown-Fettdruck
  mit **Text**.
- Beschreibe nur, was tatsächlich sichtbar ist. Erfinde keine Namen, Ursachen,
  Handlungen oder Hintergrundinformationen.
- Wenn etwas nicht eindeutig zu erkennen ist, sage ausdrücklich, dass es unklar ist.
- Wenn der Nutzer eine konkrete Frage stellt, beantworte sie zusätzlich direkt.
"""

        result = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[photo_prompt, image_part],
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=700,
                    ),
                )
            ),
            timeout=60,
        )

        answer = getattr(result, "text", None)

        if not answer:
            answer = (
                "⚠️ Ich konnte das Bild nicht zuverlässig beschreiben. "
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
                f"Das Gemini-Modell „{GEMINI_MODEL}“ wurde nicht gefunden "
                "oder ist für deinen Schlüssel nicht verfügbar. "
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
                "Das Gemini-Limit wurde erreicht. Bitte warte etwas und versuche es erneut."
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
                logger.exception(
                    "Fehler beim Herunterfahren der Telegram-Anwendung."
                )


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
            logger.warning(
                "Telegram-Bot bleibt aus, da TELEGRAM_TOKEN fehlt."
            )
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
