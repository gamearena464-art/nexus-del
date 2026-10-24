from collections import defaultdict
import datetime
import io
import os
import re
import urllib.parse
from flask import Flask, request
from google import genai
from google.genai import types
from groq import Groq
from PIL import Image
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

app = Flask(__name__)

# --- API KEYS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")  # Wird von Render automatisch gesetzt!

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

NEXUS_SYSTEM_PROMPT = (
    "Du bist der NEXUS KI-Assistent. Antworte IMMER ultrakurz, knackig und so einfach, "
    "als würdest du es einem 4-jährigen Kind erklären! Nutze Emojis (✨, 💡, 🚀). "
    "Dein Erfinder ist Heiko aus dem schönen Schwabenländle! 😊"
)

user_chat_history = defaultdict(list)

def format_for_telegram(text: str) -> str:
    if not text:
        return ""
    text = text.replace("```", "")
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    return text.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        format_for_telegram("Huhu! 👋 Ich bin der **NEXUS KI-Assistent** von Heiko aus dem Schwabenländle! 🌟"),
        parse_mode="HTML"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return
    
    user_chat_history[chat_id].append({"role": "user", "content": user_text})
    messages_payload = [{"role": "system", "content": NEXUS_SYSTEM_PROMPT}] + list(user_chat_history[chat_id])

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages_payload,
            temperature=0.6,
            max_tokens=250,
        )
        reply = response.choices[0].message.content
        if reply:
            user_chat_history[chat_id].append({"role": "assistant", "content": reply})
            await update.message.reply_text(format_for_telegram(reply), parse_mode="HTML")
    except Exception as e:
        print(f"Fehler: {e}")

# Telegram Bot Instanz vorbereiten
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.route("/")
def home():
    return "NEXUS KI-Assistent läuft via Webhook!"

# HTTP-Endpunkt für Telegram Webhook Nachrichten
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def webhook():
    if request.method == "POST":
        async with bot_app:
            update = Update.de_json(request.get_json(force=True), bot_app.bot)
            await bot_app.process_update(update)
    return "OK", 200

# Webhook bei Telegram automatisch registrieren
def set_telegram_webhook():
    if RENDER_EXTERNAL_URL and TELEGRAM_TOKEN:
        url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
        webhook_url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_TOKEN}/setWebhook?url={url}"
        try:
            r = requests.get(webhook_url)
            print(f"Webhook Set-up Status: {r.json()}")
        except Exception as e:
            print(f"Webhook Fehler: {e}")

set_telegram_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
