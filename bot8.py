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
user_premium = {}          # {user_id: {expire_at: datetime, plan_type: str}}
user_daily_views = {}      # {user_id: {date: count}}
content_packages = {}      # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
series_data = {}           # {serie_id: {"title", "photo_id", "caption", "capitulos": [video_id, ...], ...}}
current_series = {}        # {user_id: {"title", "photo_id", "caption", "serie_id", "capitulos": []}}

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
FREE_LIMIT_VIDEOS = 60
PRO_LIMIT_VIDEOS = 50
PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 1)],
}
PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 1)],
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

# --- Función auxiliar para verificar suscripción a canales ---
async def check_channel_subscription(user_id, context: ContextTypes.DEFAULT_TYPE):
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_joined.append(username)
        except Exception as e:
            logger.warning(f"Error verificando canal {username} para user {user_id}: {e}")
            not_joined.append(username) # Asumir no unido si hay error
    return not_joined

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username

    # Verificar suscripción a canales al inicio
    not_joined_channels = await check_channel_subscription(user_id, context)

    # Si el usuario NO está unido a todos los canales, pedir verificación
    if not_joined_channels:
        await update.message.reply_text(
            "👋 ¡Hola! Primero debes unirte a todos nuestros canales para usar este bot. Una vez te hayas unido, haz clic en 'Verificar suscripción' para continuar.",
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
        return # Salir de la función si no está verificado

    # Si el usuario YA está unido a todos los canales, proceder con la lógica normal o menú principal
    # Manejo del start link para mostrar sinopsis + botón "Ver Video" (Videos individuales)
    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible.")
            return

        # Mostrar sinopsis y botón "Ver Video"
        ver_video_button = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ Ver Video", callback_data=f"play_video_{pkg_id}" # Callback para cargar el video
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"🎬 **{pkg.get('caption', 'Contenido:')}**\n\nPresiona 'Ver Video' para iniciar la reproducción.",
            reply_markup=ver_video_button,
            parse_mode="Markdown"
        )
        return

    # Manejo del start link para reproducir video (Videos individuales)
    elif args and args[0].startswith("play_video_"):
        pkg_id = args[0].split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
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

        # APLICACIÓN DE LA SEGURIDAD PARA SERIES AQUÍ
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        # Si puede ver, mostrar capítulos
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
        # Si no hay argumentos específicos y el usuario ya está verificado, mostrar menú principal
        await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    not_joined = await check_channel_subscription(user_id, context)

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
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d")
