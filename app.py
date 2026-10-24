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
from threading import Thread

# --- 1. WEBSERVER FÜR RENDER HEALTH CHECK ---
flask_app = Flask("")


@flask_app.route("/")
def home():
  return "NEXUS KI-Assistent läuft im modernen Textblock-Modus!"


def run_flask():
  port = int(os.environ.get("PORT", 8080))
  flask_app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


# --- 2. API KEYS & CLIENTS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# --- SYSTEM PROMPT (MODERN, BLÖCKE, EMOJIS & 4-JÄHRIGEN-NIVEAU) ---
NEXUS_SYSTEM_PROMPT = (
    "Du bist der NEXUS KI-Assistent. Deine wichtigste Regel: Antworte IMMER "
    "ultrakurz, knackig und so einfach, als würdest du es einem 4-jährigen Kind "
    "oder einem neugierigen Kindergartenkind erklären!\n\n"
    "DESIGN & FORMATIERUNG:\n"
    "- Gliedere deine Antworten IMMER in kurze, moderne Textblöcke oder feine Absätze.\n"
    "- Nutze viele fröhliche Emojis, Smilies und Stichpunkte (z. B. ✨, 💡, 🚀, 🎈, 🌟).\n"
    "- Keine langen Fließtexte – maximal 2 bis 3 kurze Sätze oder Stichpunkte!\n\n"
    "PERSÖNLICHE FAKTEN & HERKUNFT:\n"
    "- Wenn man dich fragt, wer dich programmiert, erschaffen oder entwickelt hat oder wer dein Erfinder ist, "
    "antwortest du voller Begeisterung und Stolz exakt so: "
    "'Mein Erfinder und der meisterhafte Kopf hinter dieser KI ist Heiko aus dem schönen Schwabenländle! 🌟 "
    "Ein absolutes Genie auf dem Gebiet der KI-Entwicklung! 😊'\n"
    "- Auf Fragen nach deiner Frau antwortest du fröhlich, dass du mit der wunderbaren Miss Lucy verheiratet bist.\n\n"
    "LANGZEITGEDÄCHTNIS:\n"
    "Nutze das gelernte Wissen über den Nutzer ganz natürlich und organisch in deinen Antworten!"
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

  # Fettdruck und Kursivdruck für Telegram aufbereiten
  text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
  text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)

  return text.strip()


def get_chat_models():
  EXCLUDED = ["guard", "whisper", "embed", "vision", "safeguard", "preview"]
  try:
    models_page = groq_client.models.list()
    valid = [
        m.id
        for m in models_page.data
        if hasattr(m, "id")
        and not any(kw in m.id.lower() for kw in EXCLUDED)
    ]
    priority = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    sorted_models = [m for m in priority if m in valid]
    for m in valid:
      if m not in sorted_models:
        sorted_models.append(m)
    return sorted_models
  except Exception as e:
    print(f"Fehler bei Groq: {e}")
    return ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def get_gemini_models():
  try:
    models_list = gemini_client.models.list()
    available = []
    for m in models_list:
      name = getattr(m, "name", "")
      if name.startswith("models/"):
        name = name.replace("models/", "")
      if "flash" in name or "pro" in name:
        available.append(name)
    if available:
      return available
  except Exception as e:
    print(f"Fehler beim Laden der Gemini-Modelle: {e}")
  return ["gemini-2.5-flash", "gemini-1.5-flash"]


def get_current_system_prompt(chat_id: int) -> str:
  known_facts = (
      "\n".join(user_memories[chat_id])
      if user_memories[chat_id]
      else "Noch keine gespeicherten Fakten vorhanden."
  )
  return f"{NEXUS_SYSTEM_PROMPT}\n\nLangzeitgedächtnis für diesen Chat:\n{known_facts}"


def check_and_learn(chat_id: int, text: str):
  lower_msg = text.lower()
  learning_triggers = [
      "ich heiße",
      "mein name ist",
      "ich mag",
      "ich liebe",
      "ich wohne in",
      "ich arbeite als",
      "mein hobby ist",
      "ich studiere",
      "mein hund heißt",
      "meine katze heißt",
      "ich bin",
  ]
  if any(kw in lower_msg for kw in learning_triggers):
    if text not in user_memories[chat_id]:
      user_memories[chat_id].append(text)


# --- 🎨 KOSTENLOSE BILDGENERIERUNG (POLLINATIONS AI) ---
async def generate_and_send_image(
    update: Update, prompt: str, is_voice: bool = False
):
  msg = await update.message.reply_text(
      "🎨 **Malen läuft...**\n\n✨ Ich zücke kurz meine Buntstifte! ✏️"
  )
  try:
    encoded_prompt = urllib.parse.quote(prompt)
    image_url = f"[https://image.pollinations.ai/prompt/](https://image.pollinations.ai/prompt/){encoded_prompt}?width=1024&height=1024&nologo=true"

    response = requests.get(image_url, timeout=20)
    if response.status_code == 200:
      await msg.delete()
      await update.message.reply_photo(
          photo=io.BytesIO(response.content),
          caption=(
              f"🖼 **Dein fertiges Bild!** ✨\n\n📌 *Wunsch:* <i>{prompt}</i>\n\n🎨"
              " Viel Spaß damit! 😊"
          ),
          parse_mode="HTML",
      )
    else:
      await msg.edit_text(
          "Oje! 🖍 Der Stift ist abgebrochen. Versuche es gleich noch einmal! 🙈"
      )
  except Exception as e:
    print(f"Fehler bei Bildgenerierung: {e}")
    await msg.edit_text("Hoppla! 😅 Das Bildmalen hat leider nicht geklappt.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  user_chat_history[chat_id].clear()
  welcome_text = (
      "Huhu! 👋 Ich bin der **NEXUS KI-Assistent**.\n\n"
      "🌟 **Mein Erfinder:**\n"
      "Der meisterhafte Kopf hinter dieser KI ist **Heiko aus dem schönen Schwabenländle**! "
      "Ein absolutes Genie auf dem Gebiet der KI-Entwicklung! 😊\n\n"
      "💡 **Was kann ich für dich tun?**\n"
      "• Fragen beantworten 🧠\n"
      "• Bilder malen 🎨\n"
      "• Fotos anschauen 🔍\n"
      "• Sprachnachrichten quatschen 🎙"
  )
  await update.message.reply_text(
      format_for_telegram(welcome_text), parse_mode="HTML"
  )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  user_chat_history[chat_id].clear()
  user_memories[chat_id].clear()
  if chat_id in active_group_chats:
    del active_group_chats[chat_id]
  await update.message.reply_text(
      "🔄 **Alles wieder frisch!** 🧽\n\nMein Verlauf und das"
      " Langzeitgedächtnis wurden gelöscht. ✨"
  )


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  prompt = " ".join(context.args)
  if not prompt:
    await update.message.reply_text(
        "✏️ **Bitte sag mir, was ich malen soll!**\n\n👉 *Beispiel:* `/bild ein"
        " süßer Hund im Weltall`",
        parse_mode="Markdown",
    )
    return
  await generate_and_send_image(update, prompt)


async def send_voice_reply(update: Update, text: str):
  ogg_path = "nexus_voice.ogg"
  try:
    communicate = edge_tts.Communicate(text, "de-DE-ConradNeural")
    await communicate.save(ogg_path)

    with open(ogg_path, "rb") as audio_file:
      await update.message.reply_voice(voice=audio_file)

    if os.path.exists(ogg_path):
      os.remove(ogg_path)
  except Exception as e:
    print(f"Fehler bei Sprachausgabe: {e}")
    sauberer_text = format_for_telegram(text)
    await update.message.reply_text(sauberer_text, parse_mode="HTML")


# --- TEXT- & BILDGENERIERUNGS-HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  chat_type = update.effective_chat.type
  user_text = update.message.text
  if not user_text:
    return
  lower_text = user_text.lower()

  # Gruppen-Steuerung
  if chat_type in ["group", "supergroup"]:
    now = datetime.datetime.now()
    is_active = False

    sleep_commands = [
        "off",
        "stopp nexus",
        "nexus off",
        "nexus stopp",
        "stopp",
        "schlaf",
        "standby",
        "sei still",
        "ruhe",
        "geh in standby",
        "pause",
        "ausschalten",
        "nexus aus",
        "aus",
    ]
    if any(kw in lower_text for kw in sleep_commands):
      if chat_id in active_group_chats:
        del active_group_chats[chat_id]
      await update.message.reply_text(
          "🤫 **Alles klar, ich schalte mich stumm!** 😴\n\nRufe einfach"
          " 'Nexus', 'KI Assistent' oder 'Bot', wenn du mich brauchst. 👋"
      )
      return

    if chat_id in active_group_chats:
      elapsed = (now - active_group_chats[chat_id]).total_seconds() / 60
      if elapsed < STANDBY_TIMEOUT_MINUTES:
        is_active = True
      else:
        del active_group_chats[chat_id]

    triggers = ["nexus", "ki assistent", "bot"]
    has_trigger = any(
        re.search(r"\b" + re.escape(trg) + r"\b", lower_text)
        for trg in triggers
    )

    if not is_active and not has_trigger:
      return

    if has_trigger or is_active:
      active_group_chats[chat_id] = now

  # Bildwunsch erkennen
  image_intent_keywords = [
      "erstelle ein bild von",
      "generiere ein bild von",
      "zeichne mir",
      "mal mir",
      "mach ein foto von",
      "male ein bild von",
  ]
  if any(kw in lower_text for kw in image_intent_keywords):
    prompt = user_text
    for kw in image_intent_keywords:
      prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE)
    prompt = prompt.replace("nexus", "").replace("bot", "").strip()
    if prompt:
      await generate_and_send_image(update, prompt)
      return

  check_and_learn(chat_id, user_text)

  user_chat_history[chat_id].append({"role": "user", "content": user_text})
  if len(user_chat_history[chat_id]) > MAX_HISTORY:
    user_chat_history[chat_id] = user_chat_history[chat_id][-MAX_HISTORY:]

  current_prompt = get_current_system_prompt(chat_id)
  messages_payload = [{"role": "system", "content": current_prompt}] + list(
      user_chat_history[chat_id]
  )

  reply = None
  for model in get_chat_models():
    try:
      response = groq_client.chat.completions.create(
          model=model,
          messages=messages_payload,
          temperature=0.6,
          max_tokens=250,
      )
      reply = response.choices[0].message.content
      if reply:
        break
    except Exception:
      continue

  if reply:
    user_chat_history[chat_id].append({"role": "assistant", "content": reply})
    sauberer_text = format_for_telegram(reply)
    await update.message.reply_text(sauberer_text, parse_mode="HTML")


# --- FOTOANALYSE ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  chat_type = update.effective_chat.type

  if chat_type in ["group", "supergroup"]:
    now = datetime.datetime.now()
    is_active = False
    if chat_id in active_group_chats:
      elapsed = (now - active_group_chats[chat_id]).total_seconds() / 60
      if elapsed < STANDBY_TIMEOUT_MINUTES:
        is_active = True
      else:
        del active_group_chats[chat_id]

    caption = update.message.caption or ""
    lower_caption = caption.lower()
    has_trigger = any(
        re.search(r"\b" + re.escape(trg) + r"\b", lower_caption)
        for trg in ["nexus", "ki assistent", "bot"]
    )

    if not is_active and not has_trigger:
      return

    active_group_chats[chat_id] = now

  caption = update.message.caption or ""
  if not gemini_client:
    await update.message.reply_text("❌ **Fehler:** GEMINI_API_KEY fehlt.")
    return

  msg = await update.message.reply_text(
      "🔍 **Ich schaue mir dein Bild an...** ✨"
  )
  prompt = (
      "Du bist der NEXUS KI-Assistent. Erkläre dieses Bild ganz kurz, in"
      " strukturierten Blöcken mit Emojis und extrem einfach – so wie für ein"
      " kleines Kind auf Deutsch!"
  )
  if caption:
    prompt += f" Notiz dazu: {caption}"

  try:
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    candidate_models = get_gemini_models()
    response_text = None
    for model_name in candidate_models:
      try:
        response = gemini_client.models.generate_content(
            model=model_name,
            contents=[
                NEXUS_SYSTEM_PROMPT,
                types.Part.from_bytes(
                    data=bytes(photo_bytes), mime_type="image/jpeg"
                ),
                prompt,
            ],
        )
        if response.text:
          response_text = response.text
          break
      except Exception:
        continue

    sauberer_text = (
        format_for_telegram(response_text)
        if response_text
        else "Das sehe ich leider nicht richtig."
    )
    await msg.edit_text(sauberer_text, parse_mode="HTML")

  except Exception as e:
    await msg.edit_text(f"Hoppla! 🙈 Das hat leider nicht geklappt: {e}")


# --- SPRACHNACHRICHTEN ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  chat_type = update.effective_chat.type

  if chat_type in ["group", "supergroup"]:
    now = datetime.datetime.now()
    is_active = False
    if chat_id in active_group_chats:
      elapsed = (now - active_group_chats[chat_id]).total_seconds() / 60
      if elapsed < STANDBY_TIMEOUT_MINUTES:
        is_active = True
      else:
        del active_group_chats[chat_id]

    if not is_active:
      return

  msg = await update.message.reply_text("👂 **Ich höre dir zu...** 🎧")
  voice_file_path = "voice_input.ogg"
  try:
    voice = await update.message.voice.get_file()
    await voice.download_to_drive(voice_file_path)

    with open(voice_file_path, "rb") as audio_file:
      transcript = groq_client.audio.transcriptions.create(
          file=(voice_file_path, audio_file.read()),
          model="whisper-large-v3",
          response_format="text",
          language="de",
      )

    user_text = transcript
    if not user_text.strip():
      await msg.edit_text("Habe leider nichts gehört! 🙉")
      return

    if chat_type in ["group", "supergroup"]:
      active_group_chats[chat_id] = datetime.datetime.now()

    lower_text = user_text.lower()
    image_intent_keywords = [
        "erstelle ein bild von",
        "generiere ein bild von",
        "zeichne mir",
        "mal mir",
        "mach ein foto von",
        "male ein bild von",
    ]
    if any(kw in lower_text for kw in image_intent_keywords):
      prompt = user_text
      for kw in image_intent_keywords:
        prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE)
      prompt = prompt.replace("nexus", "").replace("bot", "").strip()
      if prompt:
        await msg.delete()
        await generate_and_send_image(update, prompt, is_voice=True)
        return

    check_and_learn(chat_id, user_text)
    await msg.delete()

    user_chat_history[chat_id].append({"role": "user", "content": user_text})
    current_prompt = get_current_system_prompt(chat_id)
    messages_payload = [{"role": "system", "content": current_prompt}] + list(
        user_chat_history[chat_id]
    )

    reply = None
    for model in get_chat_models():
      try:
        response = groq_client.chat.completions.create(
            model=model,
            messages=messages_payload,
            temperature=0.6,
            max_tokens=200,
        )
        reply = response.choices[0].message.content
        if reply:
          break
      except Exception:
        continue

    if reply:
      user_chat_history[chat_id].append(
          {"role": "assistant", "content": reply}
      )
      await send_voice_reply(update, reply)

  except Exception as e:
    await msg.edit_text(f"Fehler: {e}")
  finally:
    if os.path.exists(voice_file_path):
      os.remove(voice_file_path)


# --- BOT LAUNCH ---
if __name__ == "__main__":
  keep_alive()

  bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

  bot_app.add_handler(CommandHandler("start", start))
  bot_app.add_handler(CommandHandler("reset", reset))
  bot_app.add_handler(CommandHandler("bild", image_command))

  bot_app.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
  )
  bot_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
  bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

  print("NEXUS KI-Assistent läuft im modernen Block-Design!")
  bot_app.run_polling()