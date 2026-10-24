import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# 1. Token und Render-URL sicher aus den Environment Variables laden
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://nexus-del.onrender.com").strip()

# 2. Automatische Webhook-Registrierung beim Start
def setup_webhook():
    if not TELEGRAM_TOKEN:
        print("❌ FEHLER: Kein TELEGRAM_TOKEN in den Environment Variables gefunden!")
        return

    # Exakte Telegram-API-URL ohne Leerzeichen oder doppelte Protokolle
    webhook_target_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
    telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"

    try:
        response = requests.post(telegram_api_url, data={"url": webhook_target_url}, timeout=10)
        res_data = response.json()
        if res_data.get("ok"):
            print(f"✅ Webhook erfolgreich gesetzt auf: {webhook_target_url}")
        else:
            print(f"❌ Webhook-Fehler von Telegram: {res_data.get('description')}")
    except Exception as e:
        print(f"❌ Fehler bei der Webhook-Verbindung: {e}")

# Webhook-Einrichtung ausführen
setup_webhook()

# 3. Route für die Webhook-Benachrichtigungen von Telegram
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return jsonify({"status": "error", "message": "No JSON payload"}), 400

    # Überprüfen, ob eine Textnachricht vorliegt
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        user_text = update["message"]["text"]

        # Hier wird die Antwort logik / KI aufgerufen (Beispiel-Antwort)
        reply_text = f"Hallo! Ich habe deine Nachricht erhalten: '{user_text}'"

        # Nachricht an Telegram zurückschicken
        send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(send_url, json={"chat_id": chat_id, "text": reply_text})

    return jsonify({"status": "ok"}), 200

# 4. Status-Route zum Testen im Browser
@app.route("/", methods=["GET"])
def index():
    return "Nexus KI Assistent Server läuft perfekt!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
