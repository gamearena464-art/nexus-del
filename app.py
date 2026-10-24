import os
import io
import logging
import threading
import asyncio
from urllib.parse import quote

import requests
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from groq import Groq
from google import genai
from PIL import Image

# --------------------------------------------------
# KONFIGURATION
# --------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("NEXUS")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
)
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-2.5-flash"
)

# Flask-App: Render erwartet bei gunicorn app:app genau dieses Objekt.
app = Flask(__name__)

# Clients werden nur erstellt, wenn ein Schlüssel vorhanden ist.
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY else None
)

# Einfache Gesprächshistorie pro Telegram-Chat.
chat_histories = {}
history_lock = threading.Lock()
MAX_HISTORY_MESSAGES = 12


# --------------------------------------------------
# FLASK / RENDER HEALTH CHECK
# --------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "service": "NEXUS KI-Assistent",
        "status": "online",
        "telegram_configured": bool(TELEGRAM_TOKEN),
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# --------------------------------------------------
# TELEGRAM-BEFEHLE
# --------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "🌌 Willkommen bei NEXUS KI-Assistent!\n\n"
        "Ich kann dir bei Fragen helfen und Bilder analysieren.\n\n"
        "Befehle:\n"
        "/start – Begrüßung\n"
        "/reset – Gespräch zurücksetzen\n"
        "/bild Beschreibung – Bild erstellen\n"
        "/hilfe – Hilfe anzeigen\n\n"
        "Schreib mir einfach eine Nachricht."
    )
    await update.effective_message.reply_text(text)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "🤖 NEXUS Hilfe\n\n"
        "• Schreibe eine Frage für eine KI-Antwort.\n"
        "• Sende ein Foto, um es analysieren zu lassen.\n"
        "• Nutze /bild gefolgt von einer Bildbeschreibung.\n"
        "• Nutze /reset, um den Chatverlauf zu löschen."
    )


async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    with history_lock:
        chat_histories.pop(chat_id, None)

    await update.effective_message.reply_text(
        "♻️ Dein Gesprächsverlauf wurde zurückgesetzt."
    )


# --------------------------------------------------
# GROQ: TEXT-KI
# --------------------------------------------------

def ask_groq(chat_id: int, prompt: str) -> str:
    if not groq_client:
        return (
            "⚠️ Der Groq-API-Schlüssel fehlt. "
            "Bitte GROQ_API_KEY in Render → Environment eintragen."
        )

    with history_lock:
        history = chat_histories.get(chat_id, []).copy()

    messages = [
        {
            "role": "system",
            "content": (
                "Du bist NEXUS, ein hilfreicher, freundlicher "
                "KI-Assistent. Antworte klar und auf Deutsch, "
                "wenn der Nutzer Deutsch schreibt."
            ),
        },
        *history,
        {"role": "user", "content": prompt},
    ]

    try:
        result = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1200,
        )

        answer = result.choices[0].message.content
        answer = answer.strip() if answer else ""

        if not answer:
            answer = "Ich konnte leider keine Antwort erzeugen."

        with history_lock:
            updated = chat_histories.get(chat_id, []).copy()
            updated.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ])
            chat_histories[chat_id] = updated[-MAX_HISTORY_MESSAGES:]

        return answer

    except Exception:
        logger.exception("Fehler bei der Groq-Anfrage")
        return (
            "⚠️ Die KI-Anfrage ist fehlgeschlagen. "
            "Bitte prüfe GROQ_API_KEY und den verfügbaren Modellnamen "
            "in Render."
        )


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat_id = update.effective_chat.id
    user_text = (message.text or "").strip()

    if not user_text:
        return

    # In Gruppen nur antworten, wenn der Bot erwähnt wird
    # oder direkt auf eine Bot-Nachricht geantwortet wird.
    if update.effective_chat.type in ("group", "supergroup"):
        bot_username = context.bot.username or ""
        mentioned = (
            f"@{bot_username.lower()}" in user_text.lower()
            if bot_username else False
        )
        replied_to_bot = (
            message.reply_to_message is not None
            and message.reply_to_message.from_user is not None
            and message.reply_to_message.from_user.id == context.bot.id
        )

        if not mentioned and not replied_to_bot:
            return

        if bot_username:
            user_text = user_text.replace(
                f"@{bot_username}", ""
            ).strip()

    status = await message.reply_text("🤖 NEXUS denkt nach …")

    try:
        answer = await asyncio.to_thread(
            ask_groq, chat_id, user_text
        )
        # Telegram begrenzt Textnachrichten auf 4096 Zeichen.
        for start in range(0, len(answer), 4000):
            chunk = answer[start:start + 4000]
            if start == 0:
                await status.edit_text(chunk)
            else:
                await message.reply_text(chunk)

    except Exception:
        logger.exception("Fehler bei der Textverarbeitung")
        await status.edit_text(
            "⚠️ Es ist ein Fehler aufgetreten. Bitte versuche es erneut."
        )


# --------------------------------------------------
# GEMINI: FOTO-ANALYSE
# --------------------------------------------------

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not gemini_client:
        await message.reply_text(
            "⚠️ Für die Bildanalyse fehlt GEMINI_API_KEY "
            "in Render → Environment."
        )
        return

    status = await message.reply_text("🖼️ Ich analysiere das Bild …")

    try:
        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        prompt = (
            message.caption
            or "Beschreibe dieses Bild ausführlich auf Deutsch."
        )

        result = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=[prompt, image],
        )

        answer = (getattr(result, "text", None) or "").strip()
        if not answer:
            answer = "Ich konnte das Bild leider nicht beschreiben."

        await status.edit_text(answer[:4000])

    except Exception:
        logger.exception("Fehler bei der Gemini-Bildanalyse")
        await status.edit_text(
            "⚠️ Die Bildanalyse ist fehlgeschlagen. "
            "Bitte prüfe GEMINI_API_KEY und den Gemini-Modellnamen."
        )


# --------------------------------------------------
# /BILD: BILDER ERSTELLEN
# --------------------------------------------------

async def image_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    prompt = " ".join(context.args).strip()

    if not prompt:
        await message.reply_text(
            "Bitte gib eine Beschreibung an.\n"
            "Beispiel: /bild eine leuchtende Energie-Welt im Weltraum"
        )
        return

    status = await message.reply_text(
        "🎨 Ich bereite dein Bild vor …"
    )

    try:
        # Pollinations ist ein externer Dienst. Verfügbarkeit,
        # Modell und Nutzungsbedingungen können sich ändern.
        image_url = (
            "https://image.pollinations.ai/prompt/"
            + quote(prompt, safe="")
            + "?width=1024&height=1024&nologo=true"
        )

        response = await asyncio.to_thread(
            requests.get,
            image_url,
            timeout=90,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ValueError("Der Bilddienst lieferte kein Bild.")

        await message.reply_photo(
            photo=io.BytesIO(response.content),
            caption=f"🎨 NEXUS – {prompt[:900]}",
        )
        await status.delete()

    except Exception:
        logger.exception("Fehler bei der Bildgenerierung")
        await status.edit_text(
            "⚠️ Das Bild konnte nicht erstellt werden. "
            "Der externe Bilddienst ist möglicherweise nicht verfügbar. "
            "Bitte versuche es später erneut."
        )


# --------------------------------------------------
# UNBEKANNTE BEFEHLE / FEHLER
# --------------------------------------------------

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "Diesen Befehl kenne ich nicht. Nutze /hilfe."
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unbehandelter Telegram-Fehler",
        exc_info=context.error,
    )


# --------------------------------------------------
# TELEGRAM-BOT STARTEN
# --------------------------------------------------

def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN fehlt. "
            "Bitte in Render → Environment eintragen."
        )
        return

    try:
        telegram_app = Application.builder().token(
            TELEGRAM_TOKEN
        ).build()

        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("hilfe", help_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("reset", reset_command))
        telegram_app.add_handler(CommandHandler("bild", image_command))

        telegram_app.add_handler(
            MessageHandler(filters.PHOTO, photo_message)
        )
        telegram_app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                text_message,
            )
        )
        telegram_app.add_handler(
            MessageHandler(filters.COMMAND, unknown_command)
        )

        telegram_app.add_error_handler(error_handler)

        logger.info("NEXUS Telegram-Bot wird gestartet.")
        # stop_signals=None ist wichtig, weil der Bot in einem
        # Hintergrund-Thread gestartet wird.
        telegram_app.run_polling(stop_signals=None)

    except Exception:
        logger.exception("Der Telegram-Bot konnte nicht starten.")


# Gunicorn importiert app.py. Der Bot wird einmal im Prozess
# in einem Hintergrund-Thread gestartet.
_bot_thread = None

if TELEGRAM_TOKEN:
    _bot_thread = threading.Thread(
        target=run_telegram_bot,
        name="telegram-bot",
        daemon=True,
    )
    _bot_thread.start()
else:
    logger.warning(
        "TELEGRAM_TOKEN fehlt: Flask läuft, aber Telegram startet nicht."
    )
