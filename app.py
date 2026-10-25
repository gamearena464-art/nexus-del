
    
  
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

