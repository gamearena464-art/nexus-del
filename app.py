from collections import defaultdict
import datetime
import io
import os
import re
import urllib.parse
import edge_tts
from flask import Flask
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

# --- WEBSERVER ---
app = Flask(__name__)

@app.route("/")
def home():
    return "NEXUS KI-Assistent läuft!"

# --- API KEYS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

NEXUS_SYSTEM_PROMPT = (
    "Du bist der NEXUS KI-Assistent. Antworte IMMER ultrakurz, knackig und so einfach, "
    "als würdest du es einem 4-jährigen Kind erklären! Nutze Emojis (✨, 💡, 🚀). "
    "Dein Erfinder ist Heiko aus dem schönen Schwabenländle! 😊"
)

user_chat_history = defaultdict(list)
user_memories = defaultdict(list)
active_group_chats = {}
MAX_HISTORY = 12
STANDBY_TIMEOUT_MINUTES = 3

def format_for_telegram(text: str) -> str:
    if not text:
        return ""
    text = text.replace("```", "")
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    return text.strip()

def get_chat_models():
    try:
        return ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    except Exception:
        return ["llama-3.3-70b-versatile"]

def get_gemini_models():
    return ["gemini-2.5-flash", "gemini-1.5-flash"]

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

# --- STARTBEFEHL FÜR RENDER ---
if __name__ == "__main__":
    import threading
    
    def run_telegram_bot():
        if not TELEGRAM_TOKEN:
            print("FEHLER: TELEGRAM_TOKEN fehlt!")
            return
        bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        print("Telegram Bot gestartet (Polling)...")
        bot_app.run_polling()

    t = threading.Thread(target=run_telegram_bot)
    t.daemon = True
    t.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
