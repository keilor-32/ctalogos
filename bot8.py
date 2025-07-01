from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import os

BOT_TOKEN = '7683545410:AAHYGaGOroxqhx-ibRCdnWYpsvQXuo2oGu8'

ARCHIVO_GRUPO = "grupo_id.txt"
ARCHIVO_CANAL = "canal_id.txt"

def cargar_id(archivo):
    if os.path.exists(archivo):
        with open(archivo, "r") as f:
            return int(f.read().strip())
    return None

def guardar_id(archivo, chat_id):
    with open(archivo, "w") as f:
        f.write(str(chat_id))

grupo_destino_id = cargar_id(ARCHIVO_GRUPO)
canal_destino_id = cargar_id(ARCHIVO_CANAL)

async def guardar_id_destino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global grupo_destino_id, canal_destino_id
    chat = update.effective_chat

    if chat.type in ("group", "supergroup"):
        grupo_destino_id = chat.id
        guardar_id(ARCHIVO_GRUPO, grupo_destino_id)
        await update.message.reply_text(f"✅ Grupo registrado: {grupo_destino_id}")

    elif chat.type == "channel":
        canal_destino_id = chat.id
        guardar_id(ARCHIVO_CANAL, canal_destino_id)
        print(f"✅ Canal registrado: {canal_destino_id}")

async def reenviar_a_destinos(context, msg):
    destinos = []
    if grupo_destino_id:
        destinos.append(grupo_destino_id)
    if canal_destino_id:
        destinos.append(canal_destino_id)

    for chat_id in destinos:
        if msg.text and not (msg.photo or msg.document or msg.video or msg.voice or msg.sticker or msg.audio):
            await context.bot.send_message(chat_id=chat_id, text=msg.text)
            continue

        if msg.photo:
            await context.bot.send_photo(chat_id=chat_id, photo=msg.photo[-1].file_id, caption=msg.caption if msg.caption else msg.text)
            continue

        if msg.document:
            await context.bot.send_document(chat_id=chat_id, document=msg.document.file_id, caption=msg.caption if msg.caption else msg.text)
            continue

        if msg.video:
            await context.bot.send_video(chat_id=chat_id, video=msg.video.file_id, caption=msg.caption if msg.caption else msg.text)
            continue

        if msg.audio:
            await context.bot.send_audio(chat_id=chat_id, audio=msg.audio.file_id, caption=msg.caption if msg.caption else msg.text)
            continue

        if msg.voice:
            await context.bot.send_voice(chat_id=chat_id, voice=msg.voice.file_id, caption=msg.caption if msg.caption else msg.text)
            continue

        if msg.sticker:
            await context.bot.send_sticker(chat_id=chat_id, sticker=msg.sticker.file_id)
            if msg.text:
                await context.bot.send_message(chat_id=chat_id, text=msg.text)
            continue

        await context.bot.send_message(chat_id=chat_id, text=msg.text if msg.text else "(Mensaje no soportado)")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not grupo_destino_id and not canal_destino_id:
        await update.message.reply_text("❌ No hay grupo ni canal registrado aún.")
        return

    msg = update.message
    await reenviar_a_destinos(context, msg)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.GROUPS | filters.ChatType.CHANNEL, guardar_id_destino))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, handle_private_message))
    print("🤖 Bot funcionando...")
    app.run_polling()

if __name__ == '__main__':
    main()