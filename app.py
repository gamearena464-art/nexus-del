
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
from pathlib import Path
from datetime import datetime, timezone

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

# ============================================================
# NEXUS KI-ASSISTENT – KONFIGURATION
# ============================================================

APP_NAME = "NEXUS KI-Assistent"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "openai/gpt-oss-120b"
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

GEMINI_IMAGE_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL",
    "gemini-2.5-flash-image"
).strip()

WHISPER_MODEL = os.getenv(
    "WHISPER_MODEL",
    "whisper-large-v3"
).strip()

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "nexus_memory.db"
)

MAX_MESSAGE_LENGTH = 3900
GROUP_ACTIVE_SECONDS = 180
MAX_MEMORY_MESSAGES = 12
MAX_MEMORY_LENGTH = 1200

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("NEXUS")

# ============================================================
# CLIENTS
# ============================================================

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

app = Flask(__name__)

# ============================================================
# KI-SYSTEMPROMPT
# ============================================================

SYSTEM_PROMPT = """
Du bist NEXUS, ein freundlicher, moderner und professioneller
KI-Assistent. Du antwortest standardmäßig auf Deutsch.

DEIN SCHREIBSTIL:
- Schreibe verständlich, natürlich und freundlich.
- Erkläre Themen ausführlich genug, aber nicht unnötig lang.
- Verwende passende Emojis, normalerweise 2 bis 6 pro Antwort.
- Übertreibe es nicht mit Emojis.
- Nutze Markdown für die Formatierung deiner Antworten.
- Verwende **fette Überschriften** für wichtige Abschnitte.
- Setze nach Überschriften immer einen echten Zeilenumbruch.
- Lasse zwischen Absätzen eine Leerzeile.
- Verwende Aufzählungen, wenn sie die Erklärung übersichtlicher machen.
- Vermeide riesige, zusammenhängende Textblöcke.
- Beginne nicht jede Antwort mit einer langen Begrüßung.
- Beantworte einfache Fragen kurz und direkte Fragen direkt.
- Bei Erklärungen nutze eine klare Struktur:
  Überschrift, kurze Einführung, einzelne Schritte und Fazit.
- Verwende keine unnötigen Wiederholungen.
- Erfinde keine Fakten. Sage offen, wenn du etwas nicht weißt.

BEISPIEL FÜR DEIN FORMAT:

🚗 **Wie funktioniert ein Lenkrad?**

Das Lenkrad hilft dir, dein Auto in die gewünschte Richtung
zu steuern.

🔧 **So funktioniert es:**

- ↔️ **Drehen:** Du bewegst das Lenkrad nach links oder rechts.
- ⚙️ **Lenkung:** Die Bewegung wird auf die Räder übertragen.
- 🛣️ **Richtungswechsel:** Die Räder verändern ihre Stellung,
  und das Auto fährt in die gewünschte Richtung.

💡 **Gut zu wissen:** Eine Servolenkung unterstützt dich dabei,
das Lenkrad leichter zu drehen.

✅ **Kurz gesagt:** Das Lenkrad bestimmt die Fahrtrichtung.

WICHTIG:
- Halte die Formatierung in allen Antworten konsequent ein.
- Vermeide lange Absätze.
- Bei technischen Erklärungen gehe Schritt für Schritt vor.
- Bei Listen steht jeder Punkt in einer eigenen Zeile.
- Passe die Länge an die Frage an.
"""

# ============================================================
# SQLITE-LANGZEITGEDÄCHTNIS
# ============================================================

def get_db():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    with get_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
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

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_user
            ON memories(user_id, id)
        """)

        connection.commit()


def save_memory(user_id, role, content):
    if not content:
        return

    content = str(content)[:MAX_MEMORY_LENGTH]

    with get_db() as connection:
        connection.execute("""
            INSERT INTO memories (
                user_id, role, content, created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            str(user_id),
            role,
            content,
            datetime.now(timezone.utc).isoformat()
        ))

        connection.execute("""
            DELETE FROM memories
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id FROM memories
                  WHERE user_id = ?
                  ORDER BY id DESC
                  LIMIT 60
              )
        """, (str(user_id), str(user_id)))

        connection.commit()


def get_memories(user_id, limit=MAX_MEMORY_MESSAGES):
    with get_db() as connection:
        rows = connection.execute("""
            SELECT role, content
            FROM memories
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (str(user_id), limit)).fetchall()

    return list(reversed(rows))


def save_fact(user_id, fact):
    fact = str(fact).strip()[:500]

    if not fact:
        return

    with get_db() as connection:
        connection.execute("""
            INSERT OR IGNORE INTO facts (
                user_id, fact, created_at
            )
            VALUES (?, ?, ?)
        """, (
            str(user_id),
            fact,
            datetime.now(timezone.utc).isoformat()
        ))
        connection.commit()


def get_facts(user_id):
    with get_db() as connection:
        rows = connection.execute("""
            SELECT fact
            FROM facts
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
        """, (str(user_id),)).fetchall()

    return [row["fact"] for row in rows]


def clear_memories(user_id):
    with get_db() as connection:
        connection.execute(
            "DELETE FROM memories WHERE user_id = ?",
            (str(user_id),)
        )
        connection.execute(
            "DELETE FROM facts WHERE user_id = ?",
            (str(user_id),)
        )
        connection.commit()


# ============================================================
# MARKDOWN IN TELEGRAM-HTML UMWANDELN
# ============================================================

def markdown_to_telegram_html(text):
    """
    Wandelt einfache Markdown-Formatierungen in Telegram-HTML um.

    Zeilenumbrüche und Leerzeilen werden beibehalten.
    HTML-Sonderzeichen werden escaped.
    """

    if not text:
        return "Ich habe gerade keine Antwort. 😊"

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")

    # Codeblöcke vor der normalen Formatierung sichern.
    code_blocks = []

    def save_code_block(match):
        code = match.group(1)
        code = html.escape(code, quote=False)

        placeholder = f"NEXUSCODEBLOCK{len(code_blocks)}END"
        code_blocks.append(f"<pre>{code}</pre>")

        return placeholder

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+-]+)?[ \t]*\n?(.*?)```",
        save_code_block,
        text,
        flags=re.DOTALL
    )

    # Inline-Code sichern.
    inline_codes = []

    def save_inline_code(match):
        code = html.escape(match.group(1), quote=False)

        placeholder = f"NEXUSINLINECODE{len(inline_codes)}END"
        inline_codes.append(f"<code>{code}</code>")

        return placeholder

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # HTML-Zeichen im normalen Text sicher machen.
    text = html.escape(text, quote=False)

    # Markdown-Überschriften.
    text = re.sub(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)\s*$",
        r"<b>\1</b>",
        text
    )

    # Fett: **Text** oder __Text__
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"__(.+?)__",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL
    )

    # Kursiv: *Text* oder _Text_
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
        text
    )

    # Einfache Markdown-Links.
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        text
    )

    # Aufzählungen vereinheitlichen.
    text = re.sub(
        r"(?m)^[ \t]*[-*][ \t]+",
        "• ",
        text
    )

    # Nummerierte Listen bleiben nummeriert.
    # Mehr als zwei Leerzeilen werden auf eine Leerzeile reduziert.
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)

    # Leerzeichen am Zeilenende entfernen, Zeilenumbrüche behalten.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()

    # Gesicherte Codeblöcke wieder einsetzen.
    for index, code in enumerate(code_blocks):
        text = text.replace(
            f"NEXUSCODEBLOCK{index}END",
            code
        )

    for index, code in enumerate(inline_codes):
        text = text.replace(
            f"NEXUSINLINECODE{index}END",
            code
        )

    return text or "Ich habe gerade keine Antwort. 😊"


def html_to_plain_text(text):
    """Erzeugt einen lesbaren Text ohne HTML-Markierungen."""

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|pre|blockquote)>", "\n\n", text)
    text = re.sub(r"<li>", "• ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)

    return text.strip()


def split_html_message(text, limit=MAX_MESSAGE_LENGTH):
    """
    Teilt Text möglichst an Absatzgrenzen auf.
    HTML-Tags werden nicht absichtlich mitten im Tag getrennt.
    """

    if len(text) <= limit:
        return [text]

    paragraphs = re.split(r"(\n\n)", text)
    chunks = []
    current = ""

    for part in paragraphs:
        if len(current) + len(part) <= limit:
            current += part
            continue

        if current.strip():
            chunks.append(current.strip())
            current = ""

        if len(part) <= limit:
            current = part
            continue

        # Sehr lange einzelne Absätze vorsichtig teilen.
        # Tags können in diesem Sonderfall getrennt werden;
        # der Versand verwendet deshalb einen HTML-Fallback.
        while len(part) > limit:
            cut = part.rfind("\n", 0, limit)

            if cut < limit // 2:
                cut = limit

            chunks.append(part[:cut].strip())
            part = part[cut:].strip()

        current = part

    if current.strip():
        chunks.append(current.strip())

    return chunks or [text[:limit]]


async def send_long_message(message, text, reply_to_message_id=None):
    """
    Sendet formatierte Telegram-Nachrichten.
    Bei ungültigem HTML wird die Nachricht als Klartext gesendet.
    """

    if not text:
        text = "Ich habe gerade keine Antwort. 😊"

    formatted = markdown_to_telegram_html(text)
    chunks = split_html_message(formatted)

    for index, chunk in enumerate(chunks):
        try:
            await message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_to_message_id=(
                    reply_to_message_id if index == 0 else None
                )
            )

        except Exception as error:
            logger.warning(
                "HTML-Versand fehlgeschlagen; sende Klartext: %s",
                error
            )

            plain = html_to_plain_text(chunk)

            # Sicherer Fallback, falls ein einzelner Block zu lang ist.
            if not plain:
                plain = "Die Antwort konnte nicht formatiert werden. 😕"

            for start in range(0, len(plain), MAX_MESSAGE_LENGTH):
                await message.reply_text(
                    plain[start:start + MAX_MESSAGE_LENGTH],
                    disable_web_page_preview=True
                )


# ============================================================
# KI-ANTWORTEN: GROQ UND GEMINI-FALLBACK
# ============================================================

def build_messages(user_id, user_text):
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    facts = get_facts(user_id)

    if facts:
        facts_text = "\n".join(f"- {fact}" for fact in facts)

        messages.append({
            "role": "system",
            "content": (
                "Bekannte Informationen über diesen Nutzer. "
                "Nutze sie nur, wenn relevant:\n" + facts_text
            )
        })

    for row in get_memories(user_id):
        role = row["role"]

        if role not in ("user", "assistant"):
            continue

        messages.append({
            "role": role,
            "content": row["content"]
        })

    messages.append({
        "role": "user",
        "content": user_text
    })

    return messages


def groq_answer(messages):
    if not groq_client:
        raise RuntimeError("GROQ_API_KEY ist nicht gesetzt.")

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1400
    )

    return response.choices[0].message.content or ""


def gemini_answer(messages):
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY ist nicht gesetzt.")

    # Gemini erhält denselben Gesprächsverlauf als Text.
    conversation = []

    for item in messages:
        role = item["role"]
        content = item["content"]

        if role == "system":
            conversation.append(
                "SYSTEMANWEISUNG:\n" + content
            )
        elif role == "user":
            conversation.append(
                "NUTZER:\n" + content
            )
        else:
            conversation.append(
                "NEXUS:\n" + content
            )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents="\n\n".join(conversation)
    )

    return response.text or ""


def get_ai_answer(user_id, user_text):
    messages = build_messages(user_id, user_text)

    try:
        answer = groq_answer(messages)

        if answer.strip():
            return answer.strip()

        raise RuntimeError("Groq hat eine leere Antwort geliefert.")

    except Exception as groq_error:
        logger.exception("Groq fehlgeschlagen: %s", groq_error)

    try:
        answer = gemini_answer(messages)

        if answer.strip():
            return answer.strip()

        raise RuntimeError("Gemini hat eine leere Antwort geliefert.")

    except Exception as gemini_error:
        logger.exception("Gemini fehlgeschlagen: %s", gemini_error)

    return (
        "😕 **Ich kann gerade keine Antwort erstellen.**\n\n"
        "Bitte versuche es in einem Moment erneut. "
        "Wenn der Fehler bleibt, prüfe die API-Schlüssel und "
        "die Modellnamen in Render."
    )


# ============================================================
# SPRACHE: WHISPER-TRANSKRIPTION UND EDGE-TTS
# ============================================================

def transcribe_audio(audio_path):
    if not groq_client:
        raise RuntimeError("GROQ_API_KEY ist nicht gesetzt.")

    with open(audio_path, "rb") as audio_file:
        result = groq_client.audio.transcriptions.create(
            file=(Path(audio_path).name, audio_file.read()),
            model=WHISPER_MODEL,
            response_format="text"
        )

    if isinstance(result, str):
        return result.strip()

    return getattr(result, "text", "").strip()


async def create_tts_file(text, output_path):
    import edge_tts

    voice = os.getenv(
        "EDGE_TTS_VOICE",
        "de-DE-KillianNeural"
    )

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice
    )

    await communicate.save(output_path)


async def send_voice_reply(message, text):
    """
    Erstellt eine Sprachausgabe.
    Bei einem Fehler bleibt die Textantwort verfügbar.
    """

    plain_text = html_to_plain_text(
        markdown_to_telegram_html(text)
    )

    # TTS soll nicht endlos lange Antworten vorlesen.
    if len(plain_text) > 2500:
        plain_text = plain_text[:2450] + (
            "\n\nDie vollständige Antwort findest du oben als Text."
        )

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False
        ) as temp_file:
            audio_path = temp_file.name

        await create_tts_file(plain_text, audio_path)

        with open(audio_path, "rb") as audio_file:
            await message.reply_voice(
                voice=audio_file,
                caption="🔊 NEXUS hat dir geantwortet."
            )

    except Exception as error:
        logger.exception("Sprachausgabe fehlgeschlagen: %s", error)

        await send_long_message(
            message,
            "🔊 Die Sprachausgabe ist gerade nicht verfügbar. "
            "Hier ist meine Antwort als Text:\n\n" + text
        )

    finally:
        try:
            if "audio_path" in locals() and os.path.exists(audio_path):
                os.remove(audio_path)
        except OSError:
            pass


# ============================================================
# BILDER: ANALYSE UND GENERIERUNG
# ============================================================

def analyze_photo(image_bytes, question):
    if not gemini_client:
        return (
            "🖼️ Für die Fotoanalyse muss GEMINI_API_KEY "
            "in Render gesetzt sein."
        )

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/jpeg"
    )

    prompt = (
        "Du bist NEXUS, ein freundlicher Bildassistent. "
        "Beschreibe das Bild verständlich und strukturiert. "
        "Verwende passende Emojis, Absätze und kurze Listen. "
        "Behaupte nicht, etwas sicher zu erkennen, wenn es "
        "auf dem Bild nicht eindeutig ist.\n\n"
        "Frage des Nutzers: " + (question or "Was ist auf dem Bild?")
    )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt, image_part]
    )

    return response.text or "🖼️ Ich konnte das Bild nicht auswerten."


def generate_image(prompt):
    if not gemini_client:
        return None

    response = gemini_client.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=(
            "Erstelle ein hochwertiges Bild passend zu dieser Beschreibung: "
            + prompt
        ),
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"]
        )
    )

    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)

        for part in getattr(content, "parts", []) or []:
            inline_data = getattr(part, "inline_data", None)

            if inline_data and getattr(inline_data, "data", None):
                return inline_data.data

    return None


# ============================================================
# GRUPPENMODUS
# ============================================================

group_active_until = {}
group_last_user = {}


def is_group_chat(update):
    chat = update.effective_chat

    return bool(
        chat and chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        )
    )


def activate_group(chat_id):
    group_active_until[chat_id] = (
        time.time() + GROUP_ACTIVE_SECONDS
    )


def group_is_active(chat_id):
    return time.time() < group_active_until.get(chat_id, 0)


def deactivate_group(chat_id):
    group_active_until.pop(chat_id, None)


def remove_bot_name(text):
    return re.sub(
        r"(?i)\b(nexus|bot)\b[:,]?",
        "",
        text
    ).strip()


def should_answer_in_group(update, text, context):
    chat = update.effective_chat
    message = update.effective_message

    if not chat or not message:
        return True

    if not is_group_chat(update):
        return True

    lowered = text.lower().strip()

    stop_phrases = (
        "nexus schlafen",
        "nexus pause",
        "nexus stopp",
        "nexus stop",
        "bot schlafen",
        "bot pause",
        "bot stopp",
        "bot stop",
    )

    if any(phrase in lowered for phrase in stop_phrases):
        deactivate_group(chat.id)
        return False

    wake_phrases = (
        "nexus",
        "hey nexus",
        "hallo nexus",
        "bot",
    )

    if any(phrase in lowered for phrase in wake_phrases):
        activate_group(chat.id)
        return True

    if group_is_active(chat.id):
        return True

    # Antworten auf Nachrichten des Bots zulassen.
    replied = message.reply_to_message

    if replied and context.bot:
        if replied.from_user and replied.from_user.id == context.bot.id:
            activate_group(chat.id)
            return True

    return False


# ============================================================
# TELEGRAM-HANDLER
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message:
        return

    await send_long_message(
        message,
        "👋 **Hallo! Ich bin NEXUS.**\n\n"
        "🤖 Ich kann Fragen beantworten, Themen erklären, "
        "Bilder analysieren und – wenn die passenden Dienste "
        "konfiguriert sind – Bilder und Sprachantworten erstellen.\n\n"
        "💬 **So startest du:**\n"
        "Schreibe mir einfach deine Frage.\n\n"
        "🎙️ Du kannst mir auch eine Sprachnachricht schicken.\n\n"
        "✨ Ich helfe dir gerne!"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message:
        return

    await send_long_message(
        message,
        "📚 **NEXUS – Hilfe**\n\n"
        "💬 **Fragen:** Schreibe mir eine Nachricht.\n\n"
        "🎙️ **Sprache:** Sende mir eine Sprachnachricht.\n\n"
        "🖼️ **Bilder:** Sende ein Foto und schreibe dazu, "
        "was ich untersuchen soll.\n\n"
        "🧠 **Gedächtnis:** Ich kann den Gesprächsverlauf und "
        "gespeicherte Fakten für spätere Antworten verwenden.\n\n"
        "🗑️ **/forget:** Löscht die gespeicherten Erinnerungen "
        "und Fakten für deinen Telegram-Nutzer."
    )


async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    clear_memories(user.id)

    await send_long_message(
        message,
        "🗑️ **Deine gespeicherten Erinnerungen wurden gelöscht.**\n\n"
        "Ich starte für dich mit einem leeren Gesprächsgedächtnis."
    )


async def image_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message:
        return

    prompt = " ".join(context.args).strip()

    if not prompt:
        await send_long_message(
            message,
            "🎨 **Bild erstellen**\n\n"
            "Schreibe hinter `/bild` eine Beschreibung.\n\n"
            "Beispiel:\n"
            "`/bild Eine futuristische Stadt bei Sonnenuntergang`"
        )
        return

    if not gemini_client:
        await send_long_message(
            message,
            "⚙️ Für die Bildgenerierung muss GEMINI_API_KEY "
            "in Render gesetzt sein."
        )
        return

    await message.chat.send_action(ChatAction.UPLOAD_PHOTO)

    try:
        image_bytes = await asyncio.to_thread(
            generate_image,
            prompt
        )

        if not image_bytes:
            await send_long_message(
                message,
                "😕 Ich konnte zu dieser Beschreibung kein Bild erzeugen. "
                "Prüfe bitte auch, ob das konfigurierte Bildmodell "
                "für deinen API-Zugang verfügbar ist."
            )
            return

        await message.reply_photo(
            photo=io.BytesIO(image_bytes),
            caption="🎨 **NEXUS – Bild erstellt**",
            parse_mode=ParseMode.HTML
        )

    except Exception as error:
        logger.exception("Bildgenerierung fehlgeschlagen: %s", error)

        await send_long_message(
            message,
            "😕 **Die Bildgenerierung ist fehlgeschlagen.**\n\n"
            "Prüfe den Modellnamen und den Gemini-API-Zugang "
            "in den Render-Logs."
        )


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    text = message.text.strip()

    if not text:
        return

    if is_group_chat(update):
        if not should_answer_in_group(update, text, context):
            lowered = text.lower()

            if any(
                phrase in lowered
                for phrase in (
                    "nexus schlafen",
                    "nexus pause",
                    "nexus stopp",
                    "nexus stop",
                    "bot schlafen",
                    "bot pause",
                    "bot stopp",
                    "bot stop",
                )
            ):
                await send_long_message(
                    message,
                    "😴 **NEXUS geht in den Standby.**\n\n"
                    "Sprich mich mit „Nexus“ wieder an."
                )

            return

        text = remove_bot_name(text)

        if not text:
            await send_long_message(
                message,
                "😊 Ich bin da! Was möchtest du wissen?"
            )
            return

    # Sonderfall: Nutzer fragt nach dem Ersteller.
    if re.search(
        r"(?i)\b(wer hat dich (gemacht|erstellt|entwickelt)|"
        r"wer ist dein ersteller|wer ist dein creator)\b",
        text
    ):
        answer = "Der liebe Heiko aus dem Schwabenländle 😊"

        save_memory(user.id, "user", text)
        save_memory(user.id, "assistant", answer)

        await send_long_message(message, answer)
        return

    await message.chat.send_action(ChatAction.TYPING)

    save_memory(user.id, "user", text)

    answer = await asyncio.to_thread(
        get_ai_answer,
        user.id,
        text
    )

    save_memory(user.id, "assistant", answer)

    await send_long_message(message, answer)


async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    voice = message.voice or message.audio

    if not voice:
        return

    if not groq_client:
        await send_long_message(
            message,
            "🎙️ Für die Spracherkennung muss GROQ_API_KEY "
            "in Render gesetzt sein."
        )
        return

    await message.chat.send_action(ChatAction.TYPING)

    audio_path = None

    try:
        telegram_file = await context.bot.get_file(voice.file_id)

        suffix = ".ogg" if message.voice else ".mp3"

        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False
        ) as temp_file:
            audio_path = temp_file.name

        await telegram_file.download_to_drive(audio_path)

        recognized_text = await asyncio.to_thread(
            transcribe_audio,
            audio_path
        )

        if not recognized_text:
            await send_long_message(
                message,
                "😕 Ich konnte deine Sprachnachricht leider nicht verstehen. "
                "Versuche es bitte noch einmal."
            )
            return

        save_memory(
            user.id,
            "user",
            recognized_text
        )

        answer = await asyncio.to_thread(
            get_ai_answer,
            user.id,
            recognized_text
        )

        save_memory(
            user.id,
            "assistant",
            answer
        )

        # Sprachnachricht -> Sprachantwort.
        await send_voice_reply(message, answer)

    except Exception as error:
        logger.exception("Sprachnachricht fehlgeschlagen: %s", error)

        await send_long_message(
            message,
            "😕 **Ich konnte die Sprachnachricht nicht verarbeiten.**\n\n"
            "Bitte versuche es noch einmal oder schreibe mir deine Frage."
        )

    finally:
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
        except OSError:
            pass


async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.photo:
        return

    if not gemini_client:
        await send_long_message(
            message,
            "🖼️ Für die Fotoanalyse muss GEMINI_API_KEY "
            "in Render gesetzt sein."
        )
        return

    await message.chat.send_action(ChatAction.TYPING)

    try:
        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)

        image_bytes = await telegram_file.download_as_bytearray()

        question = message.caption or "Beschreibe das Bild verständlich."

        answer = await asyncio.to_thread(
            analyze_photo,
            bytes(image_bytes),
            question
        )

        save_memory(
            user.id,
            "user",
            "[Foto gesendet] " + question
        )
        save_memory(user.id, "assistant", answer)

        await send_long_message(message, answer)

    except Exception as error:
        logger.exception("Fotoanalyse fehlgeschlagen: %s", error)

        await send_long_message(
            message,
            "😕 **Ich konnte das Bild nicht analysieren.**\n\n"
            "Bitte versuche es noch einmal."
        )


async def handle_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Unbehandelter Telegram-Fehler",
        exc_info=context.error
    )


# ============================================================
# BOT-START
# ============================================================

async def bot_runner():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN fehlt in den Render-Umgebungsvariablen."
        )

    telegram_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", start_command)
    )
    telegram_app.add_handler(
        CommandHandler("help", help_command)
    )
    telegram_app.add_handler(
        CommandHandler("forget", forget_command)
    )
    telegram_app.add_handler(
        CommandHandler("bild", image_command)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO,
            handle_voice
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    telegram_app.add_error_handler(handle_error)

    logger.info("%s wird gestartet ...", APP_NAME)

    # Manuelles Starten, damit Flask und Telegram parallel laufen.
    await telegram_app.initialize()
    await telegram_app.start()

    if telegram_app.updater:
        await telegram_app.updater.start_polling(
            drop_pending_updates=False
        )

    logger.info("%s ist online.", APP_NAME)

    try:
        while True:
            await asyncio.sleep(3600)

    finally:
        if telegram_app.updater:
            await telegram_app.updater.stop()

        await telegram_app.stop()
        await telegram_app.shutdown()


def bot_thread_target():
    try:
        asyncio.run(bot_runner())
    except Exception:
        logger.exception("Der Telegram-Bot wurde beendet.")


def start_bot_thread():
    thread = threading.Thread(
        target=bot_thread_target,
        name="nexus-telegram-bot",
        daemon=True
    )
    thread.start()
    return thread


# ============================================================
# FLASK-ROUTEN FÜR RENDER
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "name": APP_NAME,
        "status": "online",
        "message": "NEXUS KI-Assistent läuft. 🤖"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": APP_NAME,
        "time": datetime.now(timezone.utc).isoformat()
    }), 200


# ============================================================
# PROGRAMMSTART
# ============================================================

init_database()

if __name__ == "__main__":
    # Bei mehreren Worker-Prozessen kann jeder Worker einen
    # Telegram-Poller starten. Render sollte deshalb nur einen
    # laufenden Bot-Prozess verwenden.
    start_bot_thread()

    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
