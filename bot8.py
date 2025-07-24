import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from aiohttp import web # Keep aiohttp for the web server part
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
user_premium = {}           # {user_id: {expire_at: datetime, plan_type: str}}
user_daily_views = {}       # {user_id: {date: count}}
content_packages = {}       # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
series_data = {}            # {serie_id: {"title", "photo_id", "caption", "capitulos": [video_id, ...], ...}}
current_series = {}         # {user_id: {"title", "photo_id", "caption", "serie_id", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones Firestore (Síncronas) ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, data in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        exp = data["expire_at"]
        if exp.tzinfo is None:
            batch.set(doc_ref, {"expire_at": exp.replace(tzinfo=timezone.utc).isoformat(), "plan_type": data["plan_type"]})
        else:
            batch.set(doc_ref, {"expire_at": exp.isoformat(), "plan_type": data["plan_type"]})
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            expire_at_str = data.get("expire_at")
            plan_type = data.get("plan_type", "premium_legacy")
            if expire_at_str:
                expire_at = datetime.fromisoformat(expire_at_str)
                if expire_at.tzinfo is None:
                    expire_at = expire_at.replace(tzinfo=timezone.utc)
                result[int(doc.id)] = {"expire_at": expire_at, "plan_type": plan_type}
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
    "payload": "plan_pro",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 25)],
}
PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 50)],
}

# --- Control acceso ---
def is_premium(user_id):
    if user_id in user_premium:
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "expire_at" in user_plan_data:
            return user_plan_data["expire_at"] > datetime.now(timezone.utc)
        elif isinstance(user_plan_data, datetime):
            return user_plan_data > datetime.now(timezone.utc)
    return False

def get_user_plan_type(user_id):
    if is_premium(user_id):
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "plan_type" in user_plan_data:
            return user_plan_data["plan_type"]
        return "plan_ultra"
    return "free"

def can_resend_content(user_id):
    plan_type = get_user_plan_type(user_id)
    return plan_type == "plan_ultra" or plan_type == "premium_legacy"

def can_view_video(user_id):
    plan_type = get_user_plan_type(user_id)
    today = str(datetime.utcnow().date())
    current_views = user_daily_views.get(str(user_id), {}).get(today, 0)

    if plan_type == "plan_ultra" or plan_type == "premium_legacy":
        return True
    elif plan_type == "plan_pro":
        return current_views < PRO_LIMIT_VIDEOS
    else:
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
        chapter_video_id = series_data[serie_id]["capitulos"][i]
        # Assuming channel posts or similar for direct URL
        direct_chapter_url = f"https://t.me/c/{chapter_video_id.split('_')[0]}/{chapter_video_id.split('_')[1]}" if '_' in chapter_video_id else f"{APP_URL}/series/{serie_id}/chapter_{i+1}.mp4"
        
        row.append(InlineKeyboardButton(str(i + 1), url=direct_chapter_url))
        
        if len(row) == chapters_per_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        
    buttons.append([InlineKeyboardButton("🔙 Volver al menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(buttons)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username

    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible.")
            return

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

        if can_view_video(user_id):
            await register_view(user_id)
            
            video_file_id = pkg["video_id"]
            direct_video_url = f"https://t.me/c/{video_file_id.split('_')[0]}/{video_file_id.split('_')[1]}" if '_' in video_file_id else f"{APP_URL}/videos/{pkg_id}.mp4"

            ver_video_button = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "▶️ Ver Video Directo", url=direct_video_url
                        )
                    ]
                ]
            )
            await update.message.reply_text(
                f"🎬 **{pkg.get('caption', 'Contenido:')}**\n\nPresiona 'Ver Video Directo' para iniciar la reproducción.",
                reply_markup=ver_video_button,
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
        return

    elif args and args[0].startswith("play_video_"):
        pkg_id = args[0].split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

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

    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            return

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

        if not can_view_video(user_id):
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles aún.")
            return
            
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
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d")
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
            f"• ID: {user_id}\n• Plan: {plan_type.replace('plan_', '').capitalize()}\n• Expira: {exp_date_str}",
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

    elif data.startswith("play_video_"):
        pkg_id = data.split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await query.message.reply_text("❌ Video no disponible.")
            return

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
            await query.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
            await query.message.delete()
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

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

        if can_view_video(user_id):
            await register_view(user_id)
            video_id = capitulos[index]

            botones = []
            if index > 0:
                prev_chapter_video_id = capitulos[index-1]
                prev_chapter_url = f"https://t.me/c/{prev_chapter_video_id.split('_')[0]}/{prev_chapter_video_id.split('_')[1]}" if '_' in prev_chapter_video_id else f"{APP_URL}/series/{serie_id}/chapter_{index}.mp4"
                botones.append(InlineKeyboardButton("⬅️ Anterior", url=prev_chapter_url))
            if index < total - 1:
                next_chapter_video_id = capitulos[index+1]
                next_chapter_url = f"https://t.me/c/{next_chapter_video_id.split('_')[0]}/{next_chapter_video_id.split('_')[1]}" if '_' in next_chapter_video_id else f"{APP_URL}/series/{serie_id}/chapter_{index+2}.mp4"
                botones.append(InlineKeyboardButton("➡️ Siguiente", url=next_chapter_url))
            
            current_chapter_video_id = capitulos[index]
            direct_current_chapter_url = f"https://t.me/c/{current_chapter_video_id.split('_')[0]}/{current_chapter_video_id.split('_')[1]}" if '_' in current_chapter_video_id else f"{APP_URL}/series/{serie_id}/chapter_{index+1}.mp4"
            botones.append(InlineKeyboardButton("▶️ Ver Capítulo Directo", url=direct_current_chapter_url))

            botones.append(InlineKeyboardButton("🔙 Volver a la Serie", callback_data=f"serie_list_{serie_id}"))

            markup = InlineKeyboardMarkup([botones])

            await query.edit_message_media(
                media=InputMediaVideo(
                    media=video_id,
                    caption=f"{serie['title']} - Capítulo {index+1}",
                    parse_mode="Markdown"
                ),
                reply_markup=markup,
            )
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
        
    elif data.startswith("serie_list_"):
        serie_id = data.split("_")[2]
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            return
            
        if not can_view_video(user_id):
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
    if payload == PLAN_PRO_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_pro"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Pro* se activó por 30 días.", parse_mode="Markdown")
    elif payload == PLAN_ULTRA_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_ultra"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Ultra* se activó por 30 días.", parse_mode="Markdown")
    save_data()

# --- Comandos de administración ---
async def addvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in os.getenv("ADMIN_IDS", "").split(','):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    await update.message.reply_text("Envía la *foto (portada)* y luego el *video* para el contenido, junto con la descripción/título.", parse_mode="Markdown")
    context.user_data["awaiting_content_photo"] = True
    context.user_data["temp_content"] = {}

async def addserie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in os.getenv("ADMIN_IDS", "").split(','):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return
    await update.message.reply_text("Envía la *foto (portada)* de la serie, el *título* y la *sinopsis*.\n\nEjemplo:\n`/addserie <ID_SERIE> | Título de la Serie | Sinopsis muy interesante...`", parse_mode="Markdown")

async def addcapitulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in os.getenv("ADMIN_IDS", "").split(','):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Uso: `/addcapitulo <ID_SERIE>`\n\nEnvía el *video* del capítulo después de este comando.", parse_mode="Markdown")
        return

    serie_id = context.args[0]
    if serie_id not in series_data:
        await update.message.reply_text(f"❌ La serie con ID `{serie_id}` no existe. Por favor, crea la serie primero con `/addserie`.", parse_mode="Markdown")
        return

    context.user_data["awaiting_chapter_video"] = True
    context.user_data["current_serie_id_for_chapter"] = serie_id
    await update.message.reply_text(f"Envía el *video* para el capítulo de la serie `{serie_id}`.", parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if str(user_id) in os.getenv("ADMIN_IDS", "").split(','):
        if context.user_data.get("awaiting_content_photo"):
            if update.message.photo:
                context.user_data["temp_content"]["photo_id"] = update.message.photo[-1].file_id
                await update.message.reply_text("Ahora envía el *video* para este contenido.", parse_mode="Markdown")
                context.user_data["awaiting_content_photo"] = False
                context.user_data["awaiting_content_video"] = True
            elif update.message.caption:
                context.user_data["temp_content"]["caption"] = update.message.caption
            return

        if context.user_data.get("awaiting_content_video"):
            if update.message.video:
                pkg_id = str(len(content_packages) + 1)
                context.user_data["temp_content"]["video_id"] = update.message.video.file_id
                
                if "caption" not in context.user_data["temp_content"] or not context.user_data["temp_content"]["caption"]:
                    await update.message.reply_text("Por favor, envía un *título/descripción* para este video.", parse_mode="Markdown")
                    context.user_data["awaiting_content_caption"] = True
                    return
                
                content_packages[pkg_id] = context.user_data["temp_content"]
                save_data()
                
                bot_username = (await context.bot.get_me()).username
                direct_share_url = f"https://t.me/{bot_username}?start=video_{pkg_id}"
                
                await update.message.reply_text(
                    f"✅ Video guardado con ID `{pkg_id}`.\n\n*URL de acceso (inicia el bot):* {direct_share_url}",
                    parse_mode="Markdown"
                )
                del context.user_data["awaiting_content_video"]
                del context.user_data["temp_content"]
            elif update.message.caption and context.user_data.get("awaiting_content_caption"):
                pkg_id = str(len(content_packages) + 1)
                context.user_data["temp_content"]["caption"] = update.message.caption
                content_packages[pkg_id] = context.user_data["temp_content"]
                save_data()
                
                bot_username = (await context.bot.get_me()).username
                direct_share_url = f"https://t.me/{bot_username}?start=video_{pkg_id}"

                await update.message.reply_text(
                    f"✅ Video guardado con ID `{pkg_id}`.\n\n*URL de acceso (inicia el bot):* {direct_share_url}",
                    parse_mode="Markdown"
                )
                del context.user_data["awaiting_content_video"]
                del context.user_data["awaiting_content_caption"]
                del context.user_data["temp_content"]
            return
        
        if context.user_data.get("awaiting_chapter_video") and update.message.video:
            serie_id = context.user_data["current_serie_id_for_chapter"]
            video_file_id = update.message.video.file_id
            
            if "capitulos" not in series_data[serie_id]:
                series_data[serie_id]["capitulos"] = []
            
            series_data[serie_id]["capitulos"].append(video_file_id)
            save_data()
            
            chapter_number = len(series_data[serie_id]["capitulos"])
            await update.message.reply_text(f"✅ Capítulo {chapter_number} añadido a la serie `{serie_id}`.", parse_mode="Markdown")
            del context.user_data["awaiting_chapter_video"]
            del context.user_data["current_serie_id_for_chapter"]
            return

    if update.message and update.message.text:
        if update.message.text.startswith('/addserie'):
            parts = update.message.text.split(' | ')
            if len(parts) == 3:
                serie_id = parts[0].replace('/addserie ', '').strip()
                title = parts[1].strip()
                caption = parts[2].strip()
                
                context.user_data["awaiting_serie_photo"] = True
                context.user_data["temp_serie_data"] = {
                    "serie_id": serie_id,
                    "title": title,
                    "caption": caption
                }
                await update.message.reply_text("Por favor, envía la *foto (portada)* para la serie.", parse_mode="Markdown")
                return
            else:
                await update.message.reply_text("Formato incorrecto para `/addserie`.\n\nUso: `/addserie <ID_SERIE> | Título de la Serie | Sinopsis`", parse_mode="Markdown")
                return

        if context.user_data.get("awaiting_serie_photo") and update.message.photo:
            serie_id = context.user_data["temp_serie_data"]["serie_id"]
            title = context.user_data["temp_serie_data"]["title"]
            caption = context.user_data["temp_serie_data"]["caption"]
            photo_id = update.message.photo[-1].file_id

            series_data[serie_id] = {
                "title": title,
                "caption": caption,
                "photo_id": photo_id,
                "capitulos": []
            }
            save_data()
            
            bot_username = (await context.bot.get_me()).username
            direct_share_url = f"https://t.me/{bot_username}?start=serie_{serie_id}"

            await update.message.reply_text(
                f"✅ Serie `{serie_id}` guardada con éxito.\n\n"
                f"*URL de acceso (inicia el bot):* {direct_share_url}\n\n"
                f"Ahora puedes añadir capítulos con `/addcapitulo {serie_id}`",
                parse_mode="Markdown"
            )
            del context.user_data["awaiting_serie_photo"]
            del context.user_data["temp_serie_data"]
            return

    if update.message:
        await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

# --- Webhook handler (needed if you add custom AIOHTTP routes) ---
async def webhook_handler(request):
    """Handle incoming webhook updates from Telegram."""
    # This handler is specific for python-telegram-bot's internal webhook handling.
    # The 'runner' and 'site' should be built around app_telegram.web_app
    # If you have custom AIOHTTP routes, they need to be added to app_telegram.web_app.router
    return web.Response() # This response is generally handled by PTB internally

# --- Startup and Shutdown hooks ---
async def on_startup(app):
    logger.info("AIOHTTP app starting up...")
    # Any startup logic for your custom aiohttp routes can go here
    # For instance, setting up database connections if they were outside PTB's scope

async def on_shutdown(app):
    logger.info("AIOHTTP app shutting down...")
    # Any cleanup logic for your custom aiohttp routes can go here


# --- Inicialización del bot ---
async def main():
    load_data()
    logger.info("🤖 Bot iniciado con webhook")

    # Initialize the Application
    application = Application.builder().token(TOKEN).build() # Renamed from app_telegram for clarity

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addvideo", addvideo))
    application.add_handler(CommandHandler("addserie", addserie))
    application.add_handler(CommandHandler("addcapitulo", addcapitulo))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.TEXT & ~filters.COMMAND, message_handler))

    # IMPORTANT: Add your custom aiohttp routes to the web_app created by python-telegram-bot
    # This is how you combine custom routes with the PTB webhook listener
    application.web_app.router.add_get("/ping", lambda request: web.Response(text="✅ Bot activo."))
    # You generally don't need to explicitly add "/webhook" as PTB handles it internally.
    # If you need custom startup/shutdown, apply them to application.web_app as well.
    application.web_app.on_startup.append(on_startup)
    application.web_app.on_shutdown.append(on_shutdown)


    # Set the webhook URL
    await application.bot.set_webhook(url=f"{APP_URL}/{TOKEN}")
    
    # Start the aiohttp server using the web_app provided by python-telegram-bot
    runner = web.AppRunner(application.web_app) # Use application.web_app here
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Webhook corriendo en puerto {PORT}")

    try:
        # The main loop needs to keep the event loop alive for aiohttp
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Deteniendo bot...")
    finally:
        # Clean up resources
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
