
import asyncio
import datetime as dt
import html
import io
import logging
import os
import re
import tempfile
from collections import defaultdict
from threading import Thread
from urllib.parse import quote

import edge_tts
import requests
from flask import Flask
from groq import Groq
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================ KONFIGURATION ================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nexus")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

PORT = int(os.getenv("PORT", "10000"))
MAX_HISTORY = 12
STANDBY_TIMEOUT_MINUTES = 3

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
)
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-2.5-flash"
)
TTS_VOICE = os.getenv(
    "TTS_VOICE", "de-DE-ConradNeural"
)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY else None
)

flask_app = Flask(__name__)

user_chat_history = defaultdict(list)
user_memories = defaultdict(list)
active_group_chats = {}

NEXUS_SYSTEM_PROMPT = """
Du bist der NEXUS KI-Assistent.
Antworte auf Deutsch, freundlich, verständlich und kompakt.
Verwende kurze Absätze, Stichpunkte und passende Emojis.
Erkläre schwierige Dinge einfach.
Wenn du nach dem Erfinder gefragt wirst, sage:
"Der NEXUS-Bot wurde von Heiko aus dem schönen
Schwabenländle entwickelt. 🌟"
Behaupte nicht, tatsächlich ein Mensch zu sein.
Gespeicherte Nutzerangaben sind Gesprächskontext.
""".strip()


# ================ RENDER SERVER ================

@flask_app.route("/")
def home():
    return "NEXUS KI-Assistent läuft. ✅", 200


@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


def keep_alive():
    Thread(target=run_flask, daemon=True).start()


# ================ FORMATIERUNG ================

def format_for_telegram(text):
    if not text:
        return ""

    text = text.replace("```", "")
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        r"<i>\1</i>",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


async def reply_html(message, text):
    safe_text = format_for_telegram(text) or "Ich habe gerade keine Antwort. 🙂"
    if len(safe_text) > 4000:
        safe_text = safe_text[:3990] + "…"

    await message.reply_text(safe_text, parse_mode="HTML")


# ================ GEDÄCHTNIS ================

def get_current_system_prompt(chat_id):
    facts = "\n".join(user_memories[chat_id])
    if not facts:
        facts = "Noch keine gespeicherten Fakten."

    return f"{NEXUS_SYSTEM_PROMPT}\n\nGesprächsnotizen:\n{facts}"


def check_and_learn(chat_id, text):
    triggers = (
        "ich heiße",
        "mein name ist",
        "ich mag",
        "ich liebe",
        "mein hobby ist",
        "mein hund heißt",
        "meine katze heißt",
    )

    if any(trigger in text.lower() for trigger in triggers):
        if text not in user_memories[chat_id]:
            user_memories[chat_id].append(text)
            user_memories[chat_id] = user_memories[chat_id][-20:]


def remember_turn(chat_id, role, content):
    user_chat_history[chat_id].append({
        "role": role,
        "content": content,
    })
    user_chat_history[chat_id] = user_chat_history[chat_id][-MAX_HISTORY:]


# ================ GRUPPENMODUS ================

SLEEP_COMMANDS = {
    "off", "stopp nexus", "nexus off", "nexus stopp",
    "stopp", "schlaf", "standby", "sei still", "ruhe",
    "geh in standby", "pause", "ausschalten", "nexus aus", "aus",
}


def group_should_respond(update, text):
    chat = update.effective_chat

    if not chat or chat.type not in (
        ChatType.GROUP, ChatType.SUPERGROUP
    ):
        return True

    chat_id = chat.id
    now = dt.datetime.now(dt.timezone.utc)
    lower = (text or "").lower().strip()

    if lower in SLEEP_COMMANDS:
        active_group_chats.pop(chat_id, None)
        return False

    last_active = active_group_chats.get(chat_id)
    is_active = bool(
        last_active
        and (now - last_active).total_seconds()
        < STANDBY_TIMEOUT_MINUTES * 60
    )

    has_trigger = any(
        re.search(r"\b" + re.escape(word) + r"\b", lower)
        for word in ("nexus", "ki assistent", "bot")
    )

    if is_active or has_trigger:
        active_group_chats[chat_id] = now
        return True

    return False


# ================ GROQ KI-CHAT ================

async def ask_groq(chat_id, user_text, max_tokens=250):
    if not groq_client:
        return "❌ GROQ_API_KEY fehlt in den Render-Environment-Variablen."

    check_and_learn(chat_id, user_text)
    remember_turn(chat_id, "user", user_text)

    messages = [
        {
            "role": "system",
            "content": get_current_system_prompt(chat_id),
        }
    ]
    messages.extend(user_chat_history[chat_id])

    def call_api():
        return groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=max_tokens,
        )

    try:
        response = await asyncio.to_thread(call_api)
        answer = response.choices[0].message.content or ""

        if not answer.strip():
            answer = "Ich habe gerade keine Antwort gefunden. 🙂"

        remember_turn(chat_id, "assistant", answer)
        return answer

    except Exception:
        logger.exception("Groq-Chat fehlgeschlagen")
        return (
            "❌ Die KI ist gerade nicht erreichbar. "
            "Prüfe den GROQ_API_KEY und die Render-Logs."
        )


# ================ BILDERSTELLUNG ================

async def generate_and_send_image(update, prompt):
    message = update.effective_message
    if not message:
        return

    status = await message.reply_text(
        "🎨 Ich erstelle dein Bild. Einen Moment bitte …"
    )

    try:
        image_url = (
            "https://image.pollinations.ai/prompt/"
            + quote(prompt, safe="")
            + "?width=1024&height=1024&nologo=true"
        )

        def fetch_image():
            response = requests.get(
                image_url,
                timeout=(10, 60),
                headers={"User-Agent": "NEXUS-Telegram-Bot/1.0"},
            )
            response.raise_for_status()

            if "image" not in response.headers.get("content-type", ""):
                raise ValueError("Der Bilddienst hat keine Bilddatei geliefert.")

            return response.content

        image_bytes = await asyncio.to_thread(fetch_image)
        await status.delete()

        await message.reply_photo(
            photo=io.BytesIO(image_bytes),
            filename="nexus-bild.jpg",
            caption=(
                "🖼 <b>Dein Bild ist fertig!</b>\n\n"
                f"📌 Wunsch: {html.escape(prompt)}"
            )[:1000],
            parse_mode="HTML",
        )

    except Exception:
        logger.exception("Bildgenerierung fehlgeschlagen")
        await status.edit_text(
            "🙈 Das Bild konnte gerade nicht erstellt werden. "
            "Bitte versuche es später erneut."
        )


# ================ START-BEFEHL ================

async def start(update, context):
    message = update.effective_message
    if not message:
        return

    welcome = (
        "Huhu! 👋 Ich bin der <b>NEXUS KI-Assistent</b>.\n\n"
        "🧠 Fragen beantworten\n"
        "🎨 Bilder erstellen\n"
        "🔍 Fotos analysieren\n"
        "🎙 Sprachnachrichten verstehen\n\n"
        "Bild erstellen:\n<code>/bild ein Hund im Weltall</code>\n\n"
        "Mit <code>/reset</code> löschst du den Verlauf."
    )

    await message.reply_text(welcome, parse_mode="HTML")


# ================ RESET-BEFEHL ================

async def reset(update, context):
    chat = update.effective_chat
    message = update.effective_message

    if not chat or not message:
        return

    user_chat_history.pop(chat.id, None)
    user_memories.pop(chat.id, None)
    active_group_chats.pop(chat.id, None)

    await message.reply_text(
        "🔄 Chatverlauf und temporäre Notizen gelöscht. ✨"
    )


# ================ BILDBEFEHL ================

async def image_command(update, context):
    prompt = " ".join(context.args).strip()

    if not prompt:
        await update.effective_message.reply_text(
            "✏️ Beispiel: /bild ein süßer Hund im Weltall"
        )
        return

    await generate_and_send_image(update, prompt)


# ================ SPRACHAUSGABE ================

async def send_voice_reply(update, text):
    message = update.effective_message
    if not message:
        return

    path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False
        ) as temp:
            path = temp.name

        tts = edge_tts.Communicate(text[:3500], TTS_VOICE)
        await tts.save(path)

        with open(path, "rb") as audio_file:
            await message.reply_voice(voice=audio_file)

    except Exception:
        logger.exception("Sprachausgabe fehlgeschlagen")
        await reply_html(message, text)

    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Temporäre Audiodatei konnte nicht gelöscht werden.")


# ================ BILDWUNSCH ERKENNEN ================

IMAGE_KEYWORDS = (
    "erstelle ein bild von",
    "generiere ein bild von",
    "zeichne mir",
    "mal mir",
    "male ein bild von",
)


def extract_image_prompt(text):
    for keyword in IMAGE_KEYWORDS:
        if keyword in text.lower():
            prompt = re.sub(
                re.escape(keyword), "", text, flags=re.IGNORECASE
            )
            prompt = re.sub(
                r"\b(nexus|bot)\b", "", prompt, flags=re.IGNORECASE
            )
            return prompt.strip(" ,.!?")

    return None


# ================ TEXTNACHRICHTEN ================

async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.text:
        return

    user_text = message.text.strip()

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if user_text.lower() in SLEEP_COMMANDS:
            active_group_chats.pop(chat.id, None)
            await message.reply_text(
                "🤫 Ich bin jetzt still. Erwähne „Nexus“, wenn du mich brauchst."
            )
            return

        if not group_should_respond(update, user_text):
            return

    image_prompt = extract_image_prompt(user_text)
    if image_prompt:
        await generate_and_send_image(update, image_prompt)
        return

    answer = await ask_groq(chat.id, user_text)
    await reply_html(message, answer)


# ================ FOTOANALYSE ================

async def handle_photo(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    caption = message.caption or ""

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not group_should_respond(update, caption):
            return

    if not gemini_client:
        await message.reply_text(
            "❌ GEMINI_API_KEY fehlt. Bildanalyse ist nicht eingerichtet."
        )
        return

    status = await message.reply_text("🔍 Ich schaue mir dein Bild an …")

    try:
        photo = await message.photo[-1].get_file()
        image_bytes = bytes(await photo.download_as_bytearray())

        prompt = (
            "Analysiere dieses Bild auf Deutsch. Antworte kurz, "
            "verständlich und mit passenden Emojis."
        )
        if caption:
            prompt += f"\nZusätzliche Frage: {caption}"

        def call_gemini():
            return gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    prompt,
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type="image/jpeg",
                    ),
                ],
            )

        result = await asyncio.to_thread(call_gemini)
        answer = (
            getattr(result, "text", None)
            or "Das Bild konnte ich leider nicht eindeutig analysieren."
        )

        await status.edit_text(
            format_for_telegram(answer),
            parse_mode="HTML",
        )

    except Exception:
        logger.exception("Bildanalyse fehlgeschlagen")
        await status.edit_text(
            "🙈 Die Bildanalyse ist fehlgeschlagen. "
            "Prüfe GEMINI_API_KEY und die Render-Logs."
        )


# ================ SPRACHNACHRICHTEN ================

async def handle_voice(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or not message.voice:
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not group_should_respond(update, ""):
            return

    status = await message.reply_text("👂 Ich höre mir deine Nachricht an …")
    path = None

    try:
        if not groq_client:
            await status.edit_text("❌ GROQ_API_KEY fehlt.")
            return

        with tempfile.NamedTemporaryFile(
            suffix=".ogg", delete=False
        ) as temp:
            path = temp.name

        voice_file = await message.voice.get_file()
        await voice_file.download_to_drive(path)

        def transcribe():
            with open(path, "rb") as audio_file:
                return groq_client.audio.transcriptions.create(
                    file=(os.path.basename(path), audio_file.read()),
                    model="whisper-large-v3",
                    response_format="text",
                    language="de",
                )

        transcript = str(
            await asyncio.to_thread(transcribe)
        ).strip()

        if not transcript:
            await status.edit_text("🙉 Ich habe leider nichts verstanden.")
            return

        await status.edit_text(
            "🗣 Verstanden: " + html.escape(transcript[:800]),
            parse_mode="HTML",
        )

        image_prompt = extract_image_prompt(transcript)
        if image_prompt:
            await generate_and_send_image(update, image_prompt)
            return

        answer = await ask_groq(chat.id, transcript, max_tokens=200)
        await send_voice_reply(update, answer)

    except Exception:
        logger.exception("Sprachnachricht fehlgeschlagen")
        await status.edit_text(
            "❌ Sprachnachricht fehlgeschlagen. "
            "Prüfe GROQ_API_KEY und die Render-Logs."
        )

    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Temporäre Sprachdatei konnte nicht gelöscht werden.")


# ================ FEHLERHANDLER ================

async def error_handler(update, context):
    logger.error(
        "Unbehandelter Telegram-Fehler",
        exc_info=context.error,
    )


# ================ BOT STARTEN ================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN fehlt. Bitte in Render unter Environment setzen."
        )

    keep_alive()

    bot_app: Application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("reset", reset))
    bot_app.add_handler(CommandHandler("bild", image_command))

    bot_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )
    bot_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
    bot_app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    bot_app.add_error_handler(error_handler)

    logger.info("NEXUS KI-Assistent startet mit Telegram-Polling.")

    bot_app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
