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

# ==================================================
# KONFIGURATION
# ==================================================

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
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash",
).strip()

# ==================================================
# FLASK / RENDER
# ==================================================

app = Flask(__name__)

groq_client = (
    Groq(api_key=GROQ_API_KEY, timeout=45.0, max_retries=1)
    if GROQ_API_KEY
    else None
)

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

chat_histories = {}
history_lock = threading.Lock()
MAX_HISTORY_MESSAGES = 12


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


# ==================================================
# TELEGRAM-BEFEHLE
# ==================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "🌌 Willkommen bei NEXUS KI-Assistent!\n\n"
        "Ich helfe dir bei Fragen und kann Bilder analysieren.\n\n"
        "Befehle:\n"
        "/start – Begrüßung\n"
        "/hilfe – Hilfe anzeigen\n"
        "/reset – Gespräch zurücksetzen\n"
        "/bild Beschreibung – Bild erstellen\n\n"
        "Schreib mir einfach eine Nachricht."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "🤖 NEXUS – Hilfe\n\n"
        "• Schreibe eine Frage für eine KI-Antwort.\n"
        "• Sende ein Foto, um es analysieren zu lassen.\n"
        "• Nutze /bild gefolgt von einer Bildbeschreibung.\n"
        "• Nutze /reset, um den Gesprächsverlauf zu löschen."
    )


async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    with history_lock:
        chat_histories.pop(chat.id, None)

    await message.reply_text(
        "♻️ Dein Gesprächsverlauf wurde zurückgesetzt."
    )


# ==================================================
# GROQ – TEXT-KI
# ==================================================

def ask_groq(chat_id: int, prompt: str) -> str:
    if not GROQ_API_KEY or not groq_client:
        logger.error(
            "Groq-Konfiguration fehlt: GROQ_API_KEY ist nicht gesetzt."
        )
        return (
            "⚠️ Der Groq-API-Schlüssel fehlt. "
            "Bitte GROQ_API_KEY in Render → Environment prüfen."
        )

    with history_lock:
        history = chat_histories.get(chat_id, []).copy()

    messages = [
        {
            "role": "system",
            "content": (
                "Du bist NEXUS, ein hilfreicher und freundlicher "
                "KI-Assistent. Antworte klar und verständlich. "
                "Wenn der Nutzer Deutsch schreibt, antworte auf Deutsch."
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

        logger.info("Groq-Anfrage erfolgreich.")
        return answer

    except Exception as exc:
        error_type = type(exc).__name__
        status_code = getattr(exc, "status_code", None)

        # Keine API-Schlüssel oder Request-Header protokollieren.
        logger.error(
            "Groq-Anfrage fehlgeschlagen: Typ=%s, HTTP-Status=%s, "
            "Modell=%s, Details=%s",
            error_type,
            status_code,
            GROQ_MODEL,
            str(exc)[:500],
        )

        if error_type == "AuthenticationError" or status_code == 401:
            return (
                "🔑 Groq hat den API-Schlüssel abgelehnt. "
                "Bitte GROQ_API_KEY in Render → Environment prüfen."
            )

        if status_code == 403:
            return (
                "⛔ Groq hat den Zugriff verweigert. "
                "Bitte Kontostatus und API-Berechtigungen prüfen."
            )

        if status_code == 404:
            return (
                "⚠️ Das Groq-Modell wurde nicht gefunden. "
                "Bitte GROQ_MODEL in Render → Environment prüfen."
            )

        if error_type == "RateLimitError" or status_code == 429:
            return (
                "⏳ Das Groq-Anfragelimit wurde erreicht. "
                "Bitte Kontingent und Abrechnung prüfen."
            )

        if error_type == "APIConnectionError":
            return (
                "🌐 Groq ist momentan nicht erreichbar. "
                "Bitte später erneut versuchen."
            )

        if status_code == 400:
            return (
                "⚠️ Groq hat die Anfrage abgelehnt. "
                "Bitte Modell und Anfrageparameter in den Render-Logs prüfen."
            )

        return (
            f"⚠️ Groq-Fehler: {error_type}"
            + (
                f" (HTTP {status_code})"
                if status_code is not None
                else ""
            )
            + ". Bitte Render → Logs prüfen."
        )


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    user_text = (message.text or "").strip()
    if not user_text:
        return

    # In Gruppen nur auf Erwähnungen oder Antworten an den Bot reagieren.
    if chat.type in ("group", "supergroup"):
        bot_username = context.bot.username or ""

        mentioned = (
            f"@{bot_username.lower()}" in user_text.lower()
            if bot_username
            else False
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
                f"@{bot_username}",
                "",
            ).strip()

        if not user_text:
            await message.reply_text("Was möchtest du wissen?")
            return

    status = await message.reply_text("🤖 NEXUS denkt nach …")

    try:
        answer = await asyncio.to_thread(
            ask_groq,
            chat.id,
            user_text,
        )

        # Telegram erlaubt höchstens 4096 Zeichen pro Nachricht.
        chunks = [
            answer[i:i + 4000]
            for i in range(0, len(answer), 4000)
        ] or ["Ich habe leider keine Antwort erhalten."]

        await status.edit_text(chunks[0])

        for chunk in chunks[1:]:
            await message.reply_text(chunk)

    except Exception:
        logger.exception("Fehler bei der Textverarbeitung")

        try:
            await status.edit_text(
                "⚠️ Bei der Verarbeitung ist ein Fehler aufgetreten."
            )
        except Exception:
            pass


# ==================================================
# GEMINI – BILDANALYSE
# ==================================================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

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

        image_bytes = bytes(
            await telegram_file.download_as_bytearray()
        )

        image = Image.open(
            io.BytesIO(image_bytes)
        ).convert("RGB")

        prompt = (
            message.caption.strip()
            if message.caption
            else "Beschreibe dieses Bild ausführlich auf Deutsch."
        )

        result = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=[prompt, image],
        )

        answer = (getattr(result, "text", None) or "").strip()

        if not answer:
            answer = "Ich konnte das Bild leider nicht beschreiben."

        chunks = [
            answer[i:i + 4000]
            for i in range(0, len(answer), 4000)
        ]

        await status.edit_text(chunks[0])

        for chunk in chunks[1:]:
            await message.reply_text(chunk)

    except Exception as exc:
        logger.error(
            "Gemini-Bildanalyse fehlgeschlagen: Typ=%s, Details=%s",
            type(exc).__name__,
            str(exc)[:500],
        )

        try:
            await status.edit_text(
                "⚠️ Die Bildanalyse ist fehlgeschlagen. "
                "Bitte GEMINI_API_KEY und GEMINI_MODEL in Render prüfen."
            )
        except Exception:
            pass


# ==================================================
# /BILD – BILDER ERSTELLEN
# ==================================================

async def image_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    prompt = " ".join(context.args).strip()

    if not prompt:
        await message.reply_text(
            "Bitte gib eine Bildbeschreibung an.\n\n"
            "Beispiel:\n"
            "/bild eine leuchtende Energie-Welt im Weltraum"
        )
        return

    status = await message.reply_text("🎨 Ich bereite dein Bild vor …")

    try:
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
            raise ValueError("Der Bilddienst lieferte keine Bilddatei.")

        await message.reply_photo(
            photo=io.BytesIO(response.content),
            caption=f"🎨 NEXUS – {prompt[:900]}",
        )

        await status.delete()

    except Exception as exc:
        logger.error(
            "Bildgenerierung fehlgeschlagen: Typ=%s, Details=%s",
            type(exc).__name__,
            str(exc)[:500],
        )

        try:
            await status.edit_text(
                "⚠️ Das Bild konnte nicht erstellt werden. "
                "Der externe Bilddienst ist möglicherweise nicht verfügbar. "
                "Bitte versuche es später erneut."
            )
        except Exception:
            pass


# ==================================================
# UNBEKANNTE BEFEHLE UND FEHLER
# ==================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "Diesen Befehl kenne ich nicht. Nutze /hilfe."
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if error:
        logger.error(
            "Unbehandelter Telegram-Fehler: Typ=%s, Details=%s",
            type(error).__name__,
            str(error)[:500],
        )


# ==================================================
# TELEGRAM-BOT STARTEN
# ==================================================

def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN fehlt. "
            "Bitte in Render → Environment eintragen."
        )
        return

    try:
        telegram_app = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .build()
        )

        telegram_app.add_handler(
            CommandHandler("start", start_command)
        )
        telegram_app.add_handler(
            CommandHandler("hilfe", help_command)
        )
        telegram_app.add_handler(
            CommandHandler("help", help_command)
        )
        telegram_app.add_handler(
            CommandHandler("reset", reset_command)
        )
        telegram_app.add_handler(
            CommandHandler("bild", image_command)
        )

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

        telegram_app.run_polling(stop_signals=None)

    except Exception as exc:
        logger.error(
            "Telegram-Bot konnte nicht starten: Typ=%s, Details=%s",
            type(exc).__name__,
            str(exc)[:500],
        )


# ==================================================
# HINTERGRUND-THREAD FÜR TELEGRAM
# ==================================================

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
        "TELEGRAM_TOKEN fehlt: Flask läuft, Telegram ist nicht gestartet."
    )
