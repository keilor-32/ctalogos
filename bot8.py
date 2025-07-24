import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    InputMediaVideo,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)
import firebase_admin
from firebase_admin import credentials, firestore

# --- Inicializar Firestore con variable de entorno JSON doblemente serializada ---
google_credentials_raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not google_credentials_raw:
    raise ValueError("❌ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")

google_credentials_str = json.loads(google_credentials_raw)
google_credentials_dict = json.loads(google_credentials_str)

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()
print("✅ Firestore inicializado correctamente.")

# --- Configuración ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria ---
# MODIFICADO: Ahora user_premium guarda un diccionario {expire_at: datetime, plan_type: str}
user_premium = {}       # {user_id: {expire_at: datetime, plan_type: str}}
user_daily_views = {}    # {user_id: {date: count}}
content_packages = {}    # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
series_data = {}         # {serie_id: {"title", "photo_id", "caption", "capitulos": [video_id, ...], ...}}
current_series = {}      # {user_id: {"title", "photo_id", "caption", "serie_id", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones Firestore (Síncronas) ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, data in user_premium.items(): # MODIFICADO: 'data' ahora es un dict
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        exp = data["expire_at"]
        if exp.tzinfo is None:
            batch.set(doc_ref, {"expire_at": exp.replace(tzinfo=timezone.utc).isoformat(), "plan_type": data["plan_type"]}) # MODIFICADO: Guardar plan_type
        else:
            batch.set(doc_ref, {"expire_at": exp.isoformat(), "plan_type": data["plan_type"]}) # MODIFICADO: Guardar plan_type
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            expire_at_str = data.get("expire_at")
            plan_type = data.get("plan_type", "premium_legacy") # MODIFICADO: Cargar plan_type, default para compatibilidad
            if expire_at_str:
                expire_at = datetime.fromisoformat(expire_at_str)
                if expire_at.tzinfo is None:
                    expire_at = expire_at.replace(tzinfo=timezone.utc)
                result[int(doc.id)] = {"expire_at": expire_at, "plan_type": plan_type} # MODIFICADO: Guardar como dict
        except Exception as e:
            logger.error(f"Error al cargar fecha premium para {doc.id}: {e}")
            pass
    return result

def save_videos_firestore():
    batch = db.batch()
    for pkg_id, content in content_packages.items():
        doc_ref = db.collection(COLLECTION_VIDEOS).document(pkg_id)
        batch.set(doc_ref, content)
    batch.commit()

def load_videos_firestore():
    docs = db.collection(COLLECTION_VIDEOS).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

def save_user_daily_views_firestore():
    batch = db.batch()
    for uid, views in user_daily_views.items():
        doc_ref = db.collection(COLLECTION_VIEWS).document(uid)
        batch.set(doc_ref, views)
    batch.commit()

def load_user_daily_views_firestore():
    docs = db.collection(COLLECTION_VIEWS).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

def save_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc_ref.set({"chat_ids": list(known_chats)})

def load_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        return set(data.get("chat_ids", []))
    return set()

def save_series_firestore():
    batch = db.batch()
    for serie_id, serie in series_data.items():
        doc_ref = db.collection(COLLECTION_SERIES).document(serie_id)
        batch.set(doc_ref, serie)
    batch.commit()

def load_series_firestore():
    docs = db.collection(COLLECTION_SERIES).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

# --- Guardar y cargar todo ---
def save_data():
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()

def load_data():
    global user_premium, content_packages, user_daily_views, known_chats, series_data
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()

# --- Planes ---
FREE_LIMIT_VIDEOS = 3
PRO_LIMIT_VIDEOS = 50
PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 25)],
}
PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 50)],
}

# --- Control acceso (MODIFICADO) ---
def is_premium(user_id):
    # Verifica si el usuario tiene CUALQUIER plan pago activo.
    if user_id in user_premium:
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "expire_at" in user_plan_data:
            return user_plan_data["expire_at"] > datetime.now(timezone.utc)
        # Compatibilidad con versiones antiguas donde user_premium[user_id] era solo la fecha
        elif isinstance(user_plan_data, datetime):
            return user_plan_data > datetime.now(timezone.utc)
    return False

def get_user_plan_type(user_id):
    # Obtiene el tipo de plan actual del usuario.
    if is_premium(user_id):
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "plan_type" in user_plan_data:
            return user_plan_data["plan_type"]
        # Compatibilidad: si es premium pero no tiene 'plan_type', asumir "premium_legacy" o "ultra"
        return "plan_ultra" # Asumir Ultra para planes antiguos sin tipo explícito
    return "free"

def can_resend_content(user_id):
    # SOLO el plan "ultra" (o "premium_legacy" para compatibilidad) permite reenviar.
    plan_type = get_user_plan_type(user_id)
    return plan_type == "plan_ultra" or plan_type == "premium_legacy"

def can_view_video(user_id):
    plan_type = get_user_plan_type(user_id)
    today = str(datetime.utcnow().date())
    current_views = user_daily_views.get(str(user_id), {}).get(today, 0)

    if plan_type == "plan_ultra" or plan_type == "premium_legacy":
        return True # Vistas ilimitadas
    elif plan_type == "plan_pro":
        return current_views < PRO_LIMIT_VIDEOS
    else: # plan_type == "free"
        return current_views < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    today = str(datetime.utcnow().date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_data()

# --- Canales para verificación ---
CHANNELS = {
    "canal_1": "@hsitotv",
    "canal_2": "@Jhonmaxs",
}

# --- Menú principal ---
def get_main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎧 Audio Libros", url="https://t.me/+3lDaURwlx-g4NWJk"),
                InlineKeyboardButton("📚 Libro PDF", url="https://t.me/+iJ5D1VLCAW5hYzhk"),
            ],
            [
                InlineKeyboardButton("💬 Chat Pedido", url="https://t.me/+6eA7AdRfgq81NzBh"),
                InlineKeyboardButton("📽️ doramas", url="https://t.me/+YIXdwQ9Sa-I3ODYx"),
            ],
            [
                InlineKeyboardButton("📽️ peliculas", url="https://t.me/+rvYUEq-c96kzODE0"),
                InlineKeyboardButton("🎬 series", url="https://t.me/+eYI6JZq72o4xNWFh"),
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
               ],
            [
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                InlineKeyboardButton("❓ soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- Función auxiliar para generar botones de capítulos en cuadrícula ---
def generate_chapter_buttons(serie_id, num_chapters, chapters_per_row=5):
    buttons = []
    row = []
    for i in range(num_chapters):
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"cap_{serie_id}_{i}"))
        if len(row) == chapters_per_row:
            buttons.append(row)
            row = []
    if row: # Añadir la última fila si no está completa
        buttons.append(row)
    
    # Añadir botón "Volver al menú principal" al final
    buttons.append([InlineKeyboardButton("🔙 Volver al menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(buttons)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username

    # Manejo del start link para mostrar sinopsis + enlace del Video (Videos individuales)
    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible.")
            return

        # Verifica suscripción a canales
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        # Mostrar sinopsis y ENLACE del video
        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "🎬 Aquí tienes el video completo.")
            
            # Generar el enlace del archivo para el video individual
            video_file_id = pkg["video_id"]
            # Este es un enfoque simplificado. En una aplicación real,
            # deberías generar una URL de descarga segura o usar un método
            # que devuelva la URL directa del archivo en Telegram (si es posible
            # para el bot en sí, que no siempre lo es directamente para archivos grandes).
            # Para fines de demostración y asumiendo que el bot puede reenviar el archivo
            # si no está protegido, o que el user_id puede ser parte de la URL para control.
            
            # NOTA IMPORTANTE: TELEGRAM NO PROPORCIONA ENLACES DIRECTOS A LOS ARCHIVOS
            # PARA QUE CUALQUIERA LOS COPIE Y USE FUERA DEL BOT DE FORMA SIMPLE.
            # LA FORMA MÁS COMÚN ES REENVIAR EL ARCHIVO O UN ENLACE PROFUNDO AL BOT
            # PARA QUE EL BOT LO ENVÍE.
            # Aquí, para cumplir con "enviar link y que se pueda copiar", usaremos el file_id
            # o si tuvieras un servicio externo que aloja los videos, su URL.
            # Como los videos están en Telegram (video_id), la forma más "copiable"
            # sería que el bot te lo envíe directamente para que puedas reenviar,
            # o en este caso, una "URL" que active el bot para reenviarlo (deep linking).
            
            # OPCIÓN 1: Un deep link que le dice al bot que envíe el video
            video_link = f"https://t.me/{bot_username}?start=play_video_{pkg_id}"
            
            # OPCIÓN 2 (más directa si tienes control sobre el almacenamiento o si Telegram lo permitiera así):
            # Si 'video_id' fuera una URL directa a un servicio de streaming/almacenamiento, la usarías directamente.
            # video_link = pkg["video_id"] # Esto solo si video_id fuera ya una URL http/https
            
            await update.message.reply_text(
                f"🎬 **{pkg.get('caption', 'Contenido:')}**\n\n"
                f"Para ver el video, copia y abre este enlace: \n`{video_link}`\n\n"
                "Al abrir el enlace, el bot te enviará el video directamente.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
        return

    # Manejo del start link para reproducir video (Videos individuales) - Este handler ahora enviará el video.
    elif args and args[0].startswith("play_video_"):
        pkg_id = args[0].split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        # La verificación de canales ya se hizo en el paso 'video_' anterior,
        # pero para mayor seguridad o si el usuario llegó directamente aquí, se puede repetir.
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 saludos debes unirte a todos nuestros canales para asi poder usar este bot una ves te hayas unido debes dar click en verificar suscripcion para con tinuar.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}]"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "🎬 Aquí tienes el video completo.")
            await update.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
        else:
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
        return

    # Modificado: Manejo de argumentos para series (directo a capítulos)
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            return

        # Verifica suscripción a canales (se mantiene para series)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        # APLICACIÓN DE LA SEGURIDAD PARA SERIES AQUÍ
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        # Si puede ver, mostrar sinopsis de la serie y botones de capítulos
        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles aún.")
            return
        
        # Usar la nueva función para generar los botones de los capítulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await update.message.reply_photo(
            photo=serie["photo_id"],
            caption=f"📺 *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capítulo:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "👋 ¡Hola! primero debes unirte a todos nuestros canales para usar este bot una ves te hayas unido haz click en verificar suscripcion para continuar.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"),
                        InlineKeyboardButton("🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"),
                    ],
                    [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                ]
            ),
        )

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_joined.append(username)
        except Exception:
            not_joined.append(username)
    if not not_joined:
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a:\n" + "\n".join(not_joined))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data
    bot_username = (await context.bot.get_me()).username # Obtenemos el username del bot

    if data == "planes":
        texto_planes = (
            f"💎 *Planes disponibles:*\n\n"
            f"🔹 Free – Hasta {FREE_LIMIT_VIDEOS} videos por día.\n\n"
            "🔸 *Plan Pro*\n"
            "Precio: 25 estrellas\n"
            "Beneficios: 50 videos diarios, sin reenvíos ni compartir.\n\n"
            "🔸 *Plan Ultra*\n"
            "Precio: 50 estrellas\n"
            "Beneficios: Videos y reenvíos ilimitados, sin restricciones.\n"
        )
        botones_planes = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💸 Comprar Plan Pro (25 ⭐)", callback_data="comprar_pro")],
                [InlineKeyboardButton("💸 Comprar Plan Ultra (50 ⭐)", callback_data="comprar_ultra")],
                [InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")],
            ]
        )
        await query.message.reply_text(texto_planes, parse_mode="Markdown", reply_markup=botones_planes)

    elif data == "comprar_pro":
        if is_premium(user_id):
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d") # MODIFICADO
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp_date}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_PRO_ITEM["title"],
            description=PLAN_PRO_ITEM["description"],
            payload=PLAN_PRO_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_PRO_ITEM["currency"],
            prices=PLAN_PRO_ITEM["prices"],
            start_parameter="buy-plan-pro",
        )

    elif data == "comprar_ultra":
        if is_premium(user_id):
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d") # MODIFICADO
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp_date}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_ULTRA_ITEM["title"],
            description=PLAN_ULTRA_ITEM["description"],
            payload=PLAN_ULTRA_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_ULTRA_ITEM["currency"],
            prices=PLAN_ULTRA_ITEM["prices"],
            start_parameter="buy-plan-ultra",
        )

    elif data == "perfil":
        plan_type = get_user_plan_type(user_id)
        exp_date_str = "N/A"
        if is_premium(user_id):
            exp_date = user_premium[user_id].get("expire_at")
            if exp_date:
                exp_date_str = exp_date.strftime('%Y-%m-%d')

        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan_type.replace('plan_', '').capitalize()}\n• Expira: {exp_date_str}", # MODIFICADO: Mostrar tipo de plan
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]),
        )

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

    elif data == "audio_libros":
        await query.message.reply_text("🎧 Aquí estará el contenido de Audio Libros.")
    elif data == "libro_pdf":
        await query.message.reply_text("📚 Aquí estará el contenido de Libro PDF.")
    elif data == "chat_pedido":
        await query.message.reply_text("💬 Aquí puedes hacer tu pedido en el chat.")
    elif data == "cursos":
        await query.message.reply_text("🎓 Aquí estarán los cursos disponibles.")

    # Manejo del callback para reproducir el video individual (ahora también envía el enlace copiable)
    elif data.startswith("play_video_"):
        pkg_id = data.split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await query.message.reply_text("❌ Video no disponible.")
            return

        # Verificación de seguridad (similar a 'start' handler)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await query.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await query.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "🎬 Aquí tienes el video completo.")
            
            # Generar el enlace del archivo para el video individual
            video_file_id = pkg["video_id"]
            video_link = f"https://t.me/{bot_username}?start=play_video_{pkg_id}"

            await query.message.reply_text(
                f"🎬 **{pkg.get('caption', 'Contenido:')}**\n\n"
                f"Para ver el video, copia y abre este enlace: \n`{video_link}`\n\n"
                "Al abrir el enlace, el bot te enviará el video directamente.",
                parse_mode="Markdown"
            )
            # Eliminar el mensaje anterior si se desea para no duplicar la interacción
            await query.message.delete() 
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

    # Mostrar enlace de capítulo con navegación (series)
    elif data.startswith("cap_"):
        _, serie_id, index = data.split("_")
        index = int(index)
        serie = series_data.get(serie_id)
        
        if not serie or "capitulos" not in serie:
            await query.message.reply_text("❌ Serie o capítulos no disponibles.")
            return

        capitulos = serie["capitulos"]
        total = len(capitulos)
        if index < 0 or index >= total:
            await query.message.reply_text("❌ Capítulo fuera de rango.")
            return

        # APLICACIÓN DE LA SEGURIDAD PARA CAPÍTULOS DE SERIES AQUÍ
        if can_view_video(user_id): # Verifica si tiene vistas disponibles
            await register_view(user_id) # Registra la vista
            video_id = capitulos[index]

            # Generar el enlace profundo para el capítulo de la serie
            chapter_link = f"https://t.me/{bot_username}?start=play_serie_chapter_{serie_id}_{index}"

            botones = []
            if index > 0:
                botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cap_{serie_id}_{index - 1}"))
            if index < total - 1:
                botones.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"cap_{serie_id}_{index + 1}"))
            
            # Botón "Volver a la Serie" que regresará a la lista de capítulos
            botones.append(InlineKeyboardButton("🔙 Volver a la Serie", callback_data=f"serie_list_{serie_id}")) # Nuevo callback para listar capítulos

            markup = InlineKeyboardMarkup([botones])

            await query.message.reply_text(
                f"📺 *{serie['title']}* - Capítulo {index+1}\n\n"
                f"Para ver el capítulo, copia y abre este enlace: \n`{chapter_link}`\n\n"
                "Al abrir el enlace, el bot te enviará el capítulo directamente.",
                parse_mode="Markdown",
                reply_markup=markup
            )
            # await query.message.delete() # Puedes decidir si quieres borrar el mensaje anterior o no.
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
    
    # Nuevo handler para reproducir un capítulo de serie a través de un deep link
    elif data.startswith("play_serie_chapter_"):
        _, _, _, serie_id, index = data.split("_")
        index = int(index)
        serie = series_data.get(serie_id)

        if not serie or "capitulos" not in serie:
            await query.message.reply_text("❌ Serie o capítulos no disponibles.")
            return
        
        capitulos = serie["capitulos"]
        total = len(capitulos)
        if index < 0 or index >= total:
            await query.message.reply_text("❌ Capítulo fuera de rango.")
            return

        if can_view_video(user_id):
            await register_view(user_id)
            video_id = capitulos[index]
            
            await query.message.reply_video(
                video=video_id,
                caption=f"{serie['title']} - Capítulo {index+1}",
                protect_content=not can_resend_content(user_id)
            )
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )


    # Nuevo callback para mostrar la lista de capítulos de una serie
    elif data.startswith("serie_list_"):
        serie_id = data.split("_")[2]
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            return
        
        # APLICACIÓN DE LA SEGURIDAD PARA SERIES AQUÍ (al volver a la lista)
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await query.message.reply_text("❌ Esta serie no tiene capítulos disponibles aún.")
            return
        
        # Reutilizar la función para generar los botones de los capítulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await query.edit_message_media(
            media=InputMediaPhoto(
                media=serie["photo_id"],
                caption=f"📺 *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capítulo:",
                parse_mode="Markdown"
            ),
            reply_markup=markup,
        )


# --- Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    # MODIFICADO: Guardar el tipo de plan junto con la fecha de expiración
    if payload == PLAN_PRO_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_pro"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Pro* se activó por 30 días.")
    elif payload == PLAN_ULTRA_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_ultra"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Ultra* se activó por 30 días.")
    
    save_data() # Guardar los datos actualizados de premium

# --- Comando para agregar videos (Solo para administradores) ---
async def addvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # TODO: Implementar un control de administradores real (por ejemplo, lista de IDs permitidos)
    if update.effective_user.id not in [YOUR_ADMIN_ID_HERE]: # ¡IMPORTANTE! Reemplaza con tu ID de usuario de Telegram
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    if update.message.reply_to_message and update.message.reply_to_message.video:
        video_id = update.message.reply_to_message.video.file_id
        photo_id = update.message.reply_to_message.video.thumbnail.file_id if update.message.reply_to_message.video.thumbnail else None
        caption = update.message.reply_to_message.caption or "Video sin descripción."

        # Generar un ID único para el paquete de contenido
        pkg_id = f"vid_{len(content_packages) + 1}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        content_packages[pkg_id] = {
            "video_id": video_id,
            "photo_id": photo_id,
            "caption": caption
        }
        save_data()
        
        # Generar el enlace profundo para compartir el video
        bot_username = (await context.bot.get_me()).username
        share_link = f"https://t.me/{bot_username}?start=video_{pkg_id}"
        
        await update.message.reply_text(
            f"✅ Video agregado con ID: `{pkg_id}`\n\n"
            f"🔗 Enlace para compartir: `{share_link}`\n\n"
            "Comparte este enlace para que otros puedan ver el video.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Responde a un video para agregarlo.")

# --- Comando para agregar series (Solo para administradores) ---
async def addserie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # TODO: Implementar un control de administradores real
    if update.effective_user.id not in [YOUR_ADMIN_ID_HERE]: # ¡IMPORTANTE! Reemplaza con tu ID de usuario de Telegram
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Uso: /addserie <título_de_la_serie> <descripción_de_la_serie>")
        return
    
    # Asume que el primer argumento es el título y el resto es la descripción
    title = context.args[0]
    caption = " ".join(context.args[1:])

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ Debes responder a una foto que será la portada de la serie.")
        return

    photo_id = update.message.reply_to_message.photo[-1].file_id # Usar la resolución más alta de la foto

    serie_id = f"serie_{len(series_data) + 1}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    series_data[serie_id] = {
        "title": title,
        "caption": caption,
        "photo_id": photo_id,
        "capitulos": [] # Lista para almacenar los file_id de los videos de los capítulos
    }
    save_data()
    
    # Generar el enlace profundo para compartir la serie
    bot_username = (await context.bot.get_me()).username
    share_link = f"https://t.me/{bot_username}?start=serie_{serie_id}"

    await update.message.reply_text(
        f"✅ Serie '{title}' agregada con ID: `{serie_id}`\n"
        f"Ahora puedes añadir capítulos usando /addcapítulo {serie_id}\n\n"
        f"🔗 Enlace para compartir la serie: `{share_link}`",
        parse_mode="Markdown"
    )

# --- Comando para añadir capítulos a una serie existente (Solo para administradores) ---
async def addcapitulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # TODO: Implementar un control de administradores real
    if update.effective_user.id not in [YOUR_ADMIN_ID_HERE]: # ¡IMPORTANTE! Reemplaza con tu ID de usuario de Telegram
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Uso: /addcapitulo <ID_de_la_serie> (responder a un video)")
        return
    
    serie_id = context.args[0]
    
    if serie_id not in series_data:
        await update.message.reply_text("❌ ID de serie no encontrada.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("❌ Debes responder a un video para agregarlo como capítulo.")
        return

    video_id = update.message.reply_to_message.video.file_id
    
    series_data[serie_id]["capitulos"].append(video_id)
    save_data()
    
    await update.message.reply_text(
        f"✅ Capítulo agregado a la serie '{series_data[serie_id]['title']}'.\n"
        f"Total de capítulos: {len(series_data[serie_id]['capitulos'])}"
    )

# --- Comando para obtener información (Solo para administradores) ---
async def getinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # TODO: Implementar un control de administradores real
    if update.effective_user.id not in [YOUR_ADMIN_ID_HERE]: # ¡IMPORTANTE! Reemplaza con tu ID de usuario de Telegram
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    total_users_premium = len(user_premium)
    total_videos = len(content_packages)
    total_series = len(series_data)
    
    msg = f"📊 *Estadísticas del Bot*\n\n"
    msg += f"👤 Usuarios Premium: {total_users_premium}\n"
    msg += f"🎬 Videos individuales: {total_videos}\n"
    msg += f"📺 Series: {total_series}\n\n"

    # Detalles de vistas diarias
    today = str(datetime.utcnow().date())
    total_views_today = sum(user_daily_views.get(uid, {}).get(today, 0) for uid in user_daily_views)
    msg += f"📈 Vistas hoy ({today}): {total_views_today}\n\n"
    
    # Próximas expiraciones de planes premium (ejemplo de los próximos 7 días)
    upcoming_expiries = [
        (uid, data["expire_at"].strftime('%Y-%m-%d %H:%M'), data["plan_type"])
        for uid, data in user_premium.items()
        if data["expire_at"] > datetime.now(timezone.utc) and data["expire_at"] < datetime.now(timezone.utc) + timedelta(days=7)
    ]
    if upcoming_expiries:
        msg += "⏰ Planes premium a expirar en 7 días:\n"
        for uid, exp_date, plan_type in upcoming_expiries:
            msg += f" - User ID: {uid}, Plan: {plan_type.replace('plan_', '').capitalize()}, Expira: {exp_date}\n"
    else:
        msg += "✅ No hay planes premium próximos a expirar en 7 días.\n"


    await update.message.reply_text(msg, parse_mode="Markdown")


# --- Main ---
def main():
    load_data() # Cargar datos al iniciar

    application = Application.builder().token(TOKEN).build()

    # Handlers de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addvideo", addvideo)) # Comando para administradores
    application.add_handler(CommandHandler("addserie", addserie)) # Comando para administradores
    application.add_handler(CommandHandler("addcapitulo", addcapitulo)) # Comando para administradores
    application.add_handler(CommandHandler("getinfo", getinfo)) # Comando para administradores

    # Handlers de Callbacks
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Handlers de pagos
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Iniciar el bot en modo webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=APP_URL + TOKEN
    )
    print(f"🚀 Bot iniciado en modo webhook en {APP_URL}{TOKEN}")

if __name__ == "__main__":
    # Define tu ID de administrador aquí. Es crucial para la seguridad de comandos de admin.
    # YOUR_ADMIN_ID_HERE = 1234567890 # Reemplaza con tu ID de usuario de Telegram
    # Para la revisión, dejaré un placeholder. En producción, esto debe ser tu ID real.
    # Por ejemplo, puedes obtener tu ID enviando /start a @userinfobot en Telegram.
    
    # Para que el código sea ejecutable, he definido YOUR_ADMIN_ID_HERE como una lista vacía.
    # DEBES CAMBIAR ESTO POR TU ID DE USUARIO REAL DE TELEGRAM PARA QUE LOS COMANDOS DE ADMIN FUNCIONEN.
    YOUR_ADMIN_ID_HERE = [0] # ¡CAMBIA ESTO POR TU ID DE USUARIO DE TELEGRAM!

    main()
