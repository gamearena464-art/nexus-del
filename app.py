
import asyncio
import datetime as dt
import html
import logging
import os
import re
import sqlite3
import tempfile
import threading
import urllib.parse
import io
from collections import defaultdict
from pathlib import Path

import edge_tts
import requests
from flask import Flask
from groq import Groq
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nexus")
web = Flask(__name__)


@web.get("/")
def home():
    return "NEXUS KI-Assistent läuft! 🚀", 200


@web.get("/health")
def health():
    return {"status": "ok"}, 200


def webserver():
    web.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        use_reloader=False,
    )


TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-2.5-flash"
).strip()

VOICE = os.getenv("EDGE_TTS_VOICE", "de-DE-ConradNeural")

groq = Groq(api_key=GROQ_KEY) if GROQ_KEY else None
gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

DB = (
    "/var/data/nexus_memory.sqlite3"
    if os.path.isdir("/var/data")
    else os.getenv("NEXUS_DB_PATH", "nexus_memory.sqlite3")
)

MAX_HISTORY = 12
STANDBY_MINUTES = 3

history = defaultdict(list)
active_groups = {}

SYSTEM = """
Du bist NEXUS, ein freundlicher persönlicher KI-Assistent.

Antworte auf Deutsch, außer der Nutzer schreibt in einer anderen Sprache.
Erkläre Dinge sehr einfach, wie für ein neugieriges vierjähriges Kind.

Normalerweise reichen 1 bis 3 kurze Sätze oder wenige Stichpunkte.
Nutze moderne, professionelle Formatierung und passende Emojis.

Wenn du gefragt wirst, wer dich programmiert, erschaffen,
entwickelt oder erfunden hat, antworte:

„Der liebe Heiko aus dem Schwabenländle! 😊“

Erfinde keine persönlichen Erlebnisse.
Nutze gespeicherte Erinnerungen nur, wenn sie relevant sind.
"""


# ============================================================
# DAUERHAFTES LANGZEITGEDÄCHTNIS
# ============================================================

def connect():
    return sqlite3.connect(DB, timeout=20)


def init_db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, fact)
            )
        """)
        conn.commit()


def memories(user_id):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT fact FROM memories
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (str(user_id),),
        ).fetchall()

    return [row[0] for row in reversed(rows)]


def remember(user_id, fact):
    fact = re.sub(r"\s+", " ", fact).strip()

    if not fact or len(fact) > 350:
        return

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO memories
            (user_id, fact, created_at)
            VALUES (?, ?, ?)
            """,
            (
                str(user_id),
                fact,
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def forget_all(user_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM memories WHERE user_id = ?",
            (str(user_id),),
        )
        conn.commit()


def learn(user_id, text):
    lower = text.lower()

    triggers = (
        "ich heiße ",
        "ich heisse ",
        "mein name ist ",
        "ich mag ",
        "ich liebe ",
        "ich wohne in ",
        "ich arbeite als ",
        "mein hobby ist ",
        "meine hobbys sind ",
        "ich studiere ",
        "mein hund heißt ",
        "mein hund heisst ",
        "meine katze heißt ",
        "meine katze heisst ",
        "bitte merke dir ",
        "merk dir ",
    )

    if any(trigger in lower for trigger in triggers):
        remember(user_id, text)


def system_prompt(user_id):
    facts = memories(user_id)

    memory_text = (
        "\n".join("- " + fact for fact in facts)
        if facts
        else "Noch keine gespeicherten Erinnerungen."
    )

    return (
        SYSTEM
        + "\n\nLangzeitgedächtnis des Nutzers:\n"
        + memory_text
    )


# ============================================================
# TELEGRAM-TEXT FORMATIEREN
# ============================================================

def safe_html(text):
    if not text:
        return "Da ist mir gerade nichts eingefallen. 🤔"

    text = html.escape(text.strip().replace("```", ""))[:3900]

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.S,
    )

    text = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        r"<i>\1</i>",
        text,
        flags=re.S,
    )

    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    return text


def wake(text):
    return bool(
        re.search(
            r"\b(nexus|ki[\s-]*assistent|bot)\b",
            text,
            re.I,
        )
    )


def stop_phrase(text):
    text = re.sub(
        r"[.!?,;:]+",
        " ",
        text.lower(),
    ).strip()

    exact = {
        "stopp",
        "stop",
        "nexus stopp",
        "stopp nexus",
        "nexus stop",
        "bot stopp",
        "nexus aus",
        "bot aus",
        "ausschalten",
        "geh schlafen",
        "schlaf",
        "standby",
        "sei still",
        "ruhe",
        "pause",
        "tschüss nexus",
        "tschuss nexus",
        "tschüss bot",
        "tschuss bot",
        "auf wiedersehen nexus",
        "gute nacht nexus",
        "bis später nexus",
        "bis spaeter nexus",
    }

    if text in exact:
        return True

    return bool(
        re.search(
            r"\b(tsch[uü]ss|auf wiedersehen|gute nacht|"
            r"bis später|bis spaeter)\s+(nexus|bot)\b",
            text,
        )
    )


def group_active(chat_id):
    started = active_groups.get(chat_id)

    if not started:
        return False

    elapsed = (
        dt.datetime.now(dt.timezone.utc) - started
    ).total_seconds()

    if elapsed >= STANDBY_MINUTES * 60:
        active_groups.pop(chat_id, None)
        return False

    return True


def activate(chat_id):
    active_groups[chat_id] = dt.datetime.now(dt.timezone.utc)


# ============================================================
# KI-CHAT
# ============================================================

async def ask_ai(user_id, chat_id, text):
    if not groq:
        return "❌ GROQ_API_KEY fehlt in Render → Environment."

    chat_history = history[chat_id]

    chat_history.append({
        "role": "user",
        "content": text,
    })

    del chat_history[:-MAX_HISTORY]

    try:
        response = await asyncio.to_thread(
            groq.chat.completions.create,
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt(user_id),
                }
            ] + list(chat_history),
            temperature=0.6,
            max_tokens=300,
        )

        answer = (
            response.choices[0].message.content
            or "Da ist mir gerade nichts eingefallen. 🤔"
        )

        chat_history.append({
            "role": "assistant",
            "content": answer,
        })

        del chat_history[:-MAX_HISTORY]

        return answer

    except Exception:
        log.exception("Groq-Anfrage fehlgeschlagen")

        return (
            "❌ Mein KI-Chat hat ein Problem. "
            "Prüfe GROQ_MODEL, GROQ_API_KEY und die Render-Logs. 🔧"
        )


# ============================================================
# BILDER ERSTELLEN
# ============================================================

async def make_image(update, prompt):
    message = await update.effective_message.reply_text(
        "🎨 Ich male dein Bild … ✨"
    )

    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            + urllib.parse.quote(prompt[:700], safe="")
            + "?width=1024&height=1024&nologo=true"
        )

        response = await asyncio.to_thread(
            requests.get,
            url,
            timeout=60,
        )

        response.raise_for_status()

        if not response.headers.get(
            "content-type", ""
        ).startswith("image/"):
            raise ValueError("Der Bilddienst hat kein Bild geliefert.")

        await message.delete()

        await update.effective_message.reply_photo(
            photo=io.BytesIO(response.content),
            caption=(
                f"🖼️ <b>Dein Bild ist fertig!</b>\n\n"
                f"✨ <i>{html.escape(prompt[:500])}</i>"
            ),
            parse_mode="HTML",
        )

    except Exception:
        log.exception("Bildgenerierung fehlgeschlagen")

        await message.edit_text(
            "Hoppla! 😅 Das Bild konnte gerade nicht erstellt werden. "
            "Versuch es später noch einmal."
        )


async def bild(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()

    if not prompt:
        await update.effective_message.reply_text(
            "🎨 Beispiel: /bild ein kleiner Roboter im Weltall"
        )
        return

    await make_image(update, prompt)


def image_prompt_from(text):
    starters = (
        "erstelle ein bild von",
        "generiere ein bild von",
        "zeichne mir",
        "mal mir",
        "male ein bild von",
        "mach ein bild von",
    )

    for starter in starters:
        if starter in text.lower():
            prompt = re.sub(
                re.escape(starter),
                "",
                text,
                flags=re.I,
            ).strip()

            prompt = re.sub(
                r"\b(nexus|bot)\b",
                "",
                prompt,
                flags=re.I,
            ).strip(" ,.!?")

            if prompt:
                return prompt

    return None


# ============================================================
# TEXTNACHRICHTEN
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not message.text or not chat or not user:
        return

    text = message.text.strip()
    is_group = chat.type in ("group", "supergroup")

    if is_group:
        active = group_active(chat.id)
        addressed = wake(text)

        if stop_phrase(text):
            active_groups.pop(chat.id, None)

            if active or addressed:
                await message.reply_text(
                    "🤫 Alles klar, ich gehe in den Standby. "
                    "Ruf mich mit „Nexus“ wieder, wenn du mich brauchst. 😴"
                )
            return

        if not active and not addressed:
            return

        activate(chat.id)

    prompt = image_prompt_from(text)

    if prompt:
        await make_image(update, prompt)
        return

    learn(user.id, text)

    answer = await ask_ai(user.id, chat.id, text)

    await message.reply_text(
        safe_html(answer),
        parse_mode="HTML",
    )


# ============================================================
# SPRACHE VERSTEHEN UND ANTWORTEN
# ============================================================

async def transcribe(path):
    if not groq:
        raise RuntimeError("GROQ_API_KEY fehlt")

    def run_transcription():
        with open(path, "rb") as audio_file:
            return groq.audio.transcriptions.create(
                file=(
                    os.path.basename(path),
                    audio_file.read(),
                ),
                model="whisper-large-v3",
                response_format="text",
                language="de",
            )

    result = await asyncio.to_thread(run_transcription)

    return result if isinstance(result, str) else str(result)


async def voice_reply(update, answer):
    audio_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False,
        ) as temp:
            audio_path = temp.name

        await edge_tts.Communicate(
            answer[:1800],
            VOICE,
        ).save(audio_path)

        with open(audio_path, "rb") as audio:
            await update.effective_message.reply_voice(
                voice=audio
            )

    except Exception:
        log.exception("Sprachausgabe fehlgeschlagen")

        await update.effective_message.reply_text(
            safe_html(answer),
            parse_mode="HTML",
        )

    finally:
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass


async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not message.voice or not chat or not user:
        return

    is_group = chat.type in ("group", "supergroup")

    status = await message.reply_text(
        "👂 Ich höre mir deine Nachricht an … 🎧"
    )

    audio_path = None

    try:
        voice_file = await message.voice.get_file()

        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False,
        ) as temp:
            audio_path = temp.name

        await voice_file.download_to_drive(audio_path)

        spoken = (await transcribe(audio_path)).strip()

        if not spoken:
            await status.edit_text(
                "🙉 Ich habe leider nichts verstanden."
            )
            return

        if is_group:
            active = group_active(chat.id)
            addressed = wake(spoken) or wake(message.caption or "")

            if stop_phrase(spoken):
                active_groups.pop(chat.id, None)

                await status.edit_text(
                    "🤫 Alles klar, ich gehe in den Standby. "
                    "Ruf mich mit „Nexus“ wieder. 😴"
                )
                return

            if not active and not addressed:
                await status.delete()
                return

            activate(chat.id)

        prompt = image_prompt_from(spoken)

        if prompt:
            await status.delete()
            await make_image(update, prompt)
            return

        learn(user.id, spoken)

        answer = await ask_ai(user.id, chat.id, spoken)

        await status.delete()

        # Sprachnachricht rein = Sprachantwort raus
        await voice_reply(update, answer)

    except Exception:
        log.exception("Sprachnachricht fehlgeschlagen")

        try:
            await status.edit_text(
                "❌ Ich konnte die Sprachnachricht nicht verarbeiten. "
                "Prüfe GROQ_API_KEY und die Render-Logs."
            )
        except Exception:
            pass

    finally:
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass


# ============================================================
# FOTOANALYSE
# ============================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not message.photo or not chat or not user:
        return

    caption = message.caption or ""

    if chat.type in ("group", "supergroup"):
        active = group_active(chat.id)
        addressed = wake(caption)

        if not active and not addressed:
            return

        activate(chat.id)

    if not gemini:
        await message.reply_text(
            "❌ Fotoanalyse braucht GEMINI_API_KEY in Render → Environment."
        )
        return

    status = await message.reply_text(
        "🔍 Ich schaue mir das Foto an … ✨"
    )

    try:
        photo = await message.photo[-1].get_file()
        image_data = bytes(await photo.download_as_bytearray())

        prompt = (
            "Erkläre dieses Bild auf Deutsch in 2 bis 3 kurzen, "
            "sehr einfachen Sätzen, wie für ein vierjähriges Kind. "
            "Nutze passende Emojis. Hinweis: " + caption[:500]
        )

        response = await asyncio.to_thread(
            gemini.models.generate_content,
            model=GEMINI_MODEL,
            contents=[
                SYSTEM,
                types.Part.from_bytes(
                    data=image_data,
                    mime_type="image/jpeg",
                ),
                prompt,
            ],
        )

        answer = (
            getattr(response, "text", None)
            or "Ich kann das Bild leider nicht gut erkennen. 🤔"
        )

        await status.edit_text(
            safe_html(answer),
            parse_mode="HTML",
        )

    except Exception:
        log.exception("Fotoanalyse fehlgeschlagen")

        await status.edit_text(
            "❌ Fotoanalyse fehlgeschlagen. "
            "Prüfe GEMINI_API_KEY und GEMINI_MODEL in Render."
        )


# ============================================================
# TELEGRAM-BEFEHLE
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "👋 <b>Hallo! Ich bin NEXUS.</b> 🚀\n\n"
        "🧠 Ich kann chatten und mir wichtige Dinge merken.\n"
        "🎨 Ich erstelle Bilder und erkläre Fotos.\n"
        "🎙️ Schick mir eine Sprachnachricht – ich antworte mit Sprache.\n\n"
        "💡 In Gruppen ruf mich mit „Nexus“, „Bot“ oder „KI-Assistent“.\n"
        "Mit /gedaechtnis siehst du Erinnerungen.\n"
        "Mit /merken speicherst du eine Erinnerung.\n"
        "Mit /reset löschst du Erinnerungen und Chatverlauf.",
        parse_mode="HTML",
    )


async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    chat = update.effective_chat

    if user:
        forget_all(user.id)

    if chat:
        history.pop(chat.id, None)
        active_groups.pop(chat.id, None)

    await update.effective_message.reply_text(
        "🔄 <b>Zurückgesetzt!</b> Dein Chatverlauf und "
        "deine gespeicherten Erinnerungen wurden gelöscht.",
        parse_mode="HTML",
    )


async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    facts = memories(user.id) if user else []

    if not facts:
        await update.effective_message.reply_text(
            "🧠 Ich habe noch keine Erinnerungen gespeichert."
        )
        return

    text = "\n".join(
        "• " + html.escape(fact)
        for fact in facts[-20:]
    )

    await update.effective_message.reply_text(
        "🧠 <b>Das habe ich mir gemerkt:</b>\n\n" + text,
        parse_mode="HTML",
    )


async def remember_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    fact = " ".join(context.args).strip()

    if not user:
        return

    if not fact:
        await update.effective_message.reply_text(
            "Beispiel: /merken Ich mag Natur und Offroad-Abenteuer."
        )
        return

    remember(user.id, fact)

    await update.effective_message.reply_text(
        "🧠 Alles klar, ich speichere mir das. 😊"
    )


async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    phrase = " ".join(context.args).strip()

    if not user:
        return

    if not phrase:
        await update.effective_message.reply_text(
            "Beispiel: /vergiss Natur"
        )
        return

    with connect() as conn:
        conn.execute(
            """
            DELETE FROM memories
            WHERE user_id = ?
            AND lower(fact) LIKE lower(?)
            """,
            (str(user.id), "%" + phrase + "%"),
        )
        conn.commit()

    await update.effective_message.reply_text(
        "🧽 Passende Erinnerungen wurden gelöscht, "
        "falls welche gefunden wurden."
    )


# ============================================================
# BOT STARTEN
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN fehlt in Render → Environment."
        )

    init_db()

    threading.Thread(
        target=webserver,
        daemon=True,
    ).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("gedaechtnis", memory_command))
    app.add_handler(CommandHandler("merken", remember_command))
    app.add_handler(CommandHandler("vergiss", forget_command))
    app.add_handler(CommandHandler("bild", bild))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    log.info("NEXUS startet. SQLite-Datei: %s", DB)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
