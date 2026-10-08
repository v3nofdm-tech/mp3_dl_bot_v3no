import os
import io
import time
import asyncio
import logging
from pathlib import Path
from io import BytesIO

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaAudio,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, APIC, ID3NoHeaderError

# ── CONFIGURATION ──────────────────────────────────────────────────
BOT_TOKEN = "8618131684:AAGQmyd-F-5TcDilO4lZUu_WH3HQVxhYyXw"
TMPDIR    = Path(__import__("tempfile").gettempdir()) / "v3no_tagger"
TMPDIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("V3noTagger")

# ── ETATS DE LA CONVERSATION ────────────────────────────────────────
EDIT_MENU = 0
WAITING_TEXT = 1
WAITING_COVER = 2

# ── FONCTIONS TAGS ─────────────────────────────────────────────────
def read_tags(path: Path) -> dict:
    try:
        audio = MP3(str(path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        T = audio.tags
    except Exception:
        T = ID3()

    def s(k): return str(T.get(k, "")) or ""
    
    cover_bytes = None
    for k, v in T.items():
        if k.startswith("APIC"):
            cover_bytes = v.data
            break

    return {
        "title": s("TIT2") or path.stem,
        "artist": s("TPE1"),
        "album": s("TALB"),
        "year": s("TDRC")[:4],
        "genre": s("TCON"),
        "track": s("TRCK"),
        "cover": cover_bytes,
        "filename": path.name
    }

def write_tags(path: Path, tags: dict):
    try:
        audio = MP3(str(path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        T = audio.tags
    except Exception:
        T = ID3()

    if tags.get("title"):  T["TIT2"] = TIT2(encoding=3, text=tags["title"])
    if tags.get("artist"): T["TPE1"] = TPE1(encoding=3, text=tags["artist"])
    if tags.get("album"):  T["TALB"] = TALB(encoding=3, text=tags["album"])
    if tags.get("year"):   T["TDRC"] = TDRC(encoding=3, text=tags["year"])
    if tags.get("genre"):  T["TCON"] = TCON(encoding=3, text=tags["genre"])
    if tags.get("track"):  T["TRCK"] = TRCK(encoding=3, text=tags["track"])
    
    if tags.get("cover"):
        # Supprimer les anciennes covers
        keys_to_del = [k for k in T.keys() if k.startswith("APIC")]
        for k in keys_to_del: del T[k]
        
        T.add(APIC(
            encoding=0, # Latin1
            mime="image/jpeg", # Supporte JPEG ou PNG selon le fichier
            type=3, # 3 is for the cover(front)
            desc="Cover",
            data=tags["cover"]
        ))
    
    T.save(str(path), v2_version=3)

# ── INTERFACE UTILISATEUR ──────────────────────────────────────────
def _tag_kb(tags: dict) -> InlineKeyboardMarkup:
    has_cover = "✅ Oui" if tags.get("cover") else "❌ Non"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 Titre : {tags['title'][:30]}", callback_data="set_title")],
        [InlineKeyboardButton(f"🎤 Artiste : {tags['artist'][:30] or '—'}", callback_data="set_artist")],
        [InlineKeyboardButton(f"💿 Album : {tags['album'][:30] or '—'}", callback_data="set_album")],
        [
            InlineKeyboardButton(f"📅 Année : {tags['year'] or '—'}", callback_data="set_year"),
            InlineKeyboardButton(f"🔢 Piste : {tags['track'] or '—'}", callback_data="set_track")
        ],
        [InlineKeyboardButton(f"🎸 Genre : {tags['genre'] or '—'}", callback_data="set_genre")],
        [InlineKeyboardButton(f"🖼️ Cover (Image) : {has_cover}", callback_data="set_cover")],
        [
            InlineKeyboardButton("✅ Sauvegarder", callback_data="save"),
            InlineKeyboardButton("❌ Annuler", callback_data="cancel")
        ]
    ])

def _get_menu_text(tags: dict) -> str:
    return (
        f"🎧 *V3NO TAG EDITOR UHQ*\n\n"
        f"Fichier : `{tags['filename']}`\n\n"
        f"Choisis un champ à modifier ci-dessous :"
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎛 *V3no Tag Editor UHQ*\n\n"
        "Bienvenue dans l'éditeur de tags MP3 ultime.\n"
        "Envoie-moi simplement un fichier `.mp3` pour commencer à l'éditer !\n\n"
        "• Titre, Artiste, Album, Année, Genre\n"
        "• Numéro de piste\n"
        "• Ajout ou modification de la Cover Art (Pochette)",
        parse_mode="Markdown"
    )

async def handle_audio_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio or update.message.document
    if not audio: return ConversationHandler.END
    
    fname = getattr(audio, "file_name", None) or "track.mp3"
    if not (fname.lower().endswith(".mp3") or
            getattr(audio, "mime_type", "") in ("audio/mpeg", "audio/mp3")):
        await update.message.reply_text("❌ Veuillez envoyer un fichier *.mp3*", parse_mode="Markdown")
        return ConversationHandler.END

    chat_id = str(update.effective_chat.id)
    edt_dir = TMPDIR / chat_id
    edt_dir.mkdir(parents=True, exist_ok=True)
    
    msg = await update.message.reply_text("⏳ Chargement du fichier...")
    
    tg_file = await audio.get_file()
    
    # Save with unique name to prevent collisions
    uid = __import__("uuid").uuid4().hex[:8]
    local = edt_dir / f"{uid}_{fname}"
    await tg_file.download_to_drive(str(local))
    
    tags = read_tags(local)
    
    ctx.user_data.clear()
    ctx.user_data.update(
        edit_file=str(local),
        tags=tags,
        msg_id=msg.message_id
    )
    
    await msg.edit_text(
        _get_menu_text(tags),
        reply_markup=_tag_kb(tags),
        parse_mode="Markdown"
    )
    return EDIT_MENU

async def handle_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    tags = ctx.user_data.get("tags")
    
    if not tags:
        await q.message.edit_text("❌ Session expirée. Renvoyez le fichier MP3.")
        return ConversationHandler.END

    if data.startswith("set_"):
        field = data.split("_")[1]
        
        if field == "cover":
            await q.message.edit_text(
                "🖼️ *Modification de la Cover*\n\n"
                "Envoyez-moi une image (photo) pour remplacer la pochette de l'album, ou tapez /cancel pour annuler.",
                parse_mode="Markdown"
            )
            return WAITING_COVER
        else:
            ctx.user_data["edit_field"] = field
            labels = {
                "title": "le titre", "artist": "l'artiste", "album": "l'album",
                "year": "l'année", "track": "le numéro de piste", "genre": "le genre"
            }
            await q.message.edit_text(
                f"✍️ Envoyez le nouveau texte pour **{labels[field]}** :\n*(ou /cancel pour annuler)*",
                parse_mode="Markdown"
            )
            return WAITING_TEXT

    if data == "cancel":
        _cleanup(ctx)
        await q.message.edit_text("❌ Édition annulée.")
        return ConversationHandler.END

    if data == "save":
        fp = Path(ctx.user_data.get("edit_file", ""))
        if not fp.exists():
            await q.message.edit_text("❌ Erreur : Fichier d'origine introuvable.")
            return ConversationHandler.END
            
        await q.message.edit_text("⏳ Application des modifications et envoi en cours...")
        
        # Apply tags
        write_tags(fp, tags)
        
        # Send back
        caption = f"🎵 *{tags['title']}*"
        if tags['artist']: caption += f"\n🎤 {tags['artist']}"
        if tags['album']:  caption += f"\n💿 {tags['album']}"
        
        with open(fp, "rb") as f:
            kw = dict(
                audio=f,
                caption=caption,
                parse_mode="Markdown",
                title=tags["title"],
                performer=tags["artist"],
                read_timeout=120,
                write_timeout=120
            )
            if tags["cover"]:
                kw["thumbnail"] = BytesIO(tags["cover"])
                
            await q.message.reply_audio(**kw)
            
        _cleanup(ctx)
        await q.message.delete()
        return ConversationHandler.END

    return EDIT_MENU

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    field = ctx.user_data.get("edit_field")
    if not field: return EDIT_MENU
    
    val = update.message.text.strip()
    ctx.user_data["tags"][field] = val
    ctx.user_data["edit_field"]  = None
    
    tags = ctx.user_data["tags"]
    
    # Try to delete user message to keep chat clean
    try: await update.message.delete()
    except Exception: pass
    
    msg_id = ctx.user_data.get("msg_id")
    try:
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg_id,
            text=_get_menu_text(tags),
            reply_markup=_tag_kb(tags),
            parse_mode="Markdown"
        )
    except Exception as e:
        # Fallback if message is too old
        m = await update.message.reply_text(
            _get_menu_text(tags),
            reply_markup=_tag_kb(tags),
            parse_mode="Markdown"
        )
        ctx.user_data["msg_id"] = m.message_id
        
    return EDIT_MENU

async def handle_cover_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo
    document = update.message.document
    
    if not photo and not document:
        await update.message.reply_text("❌ Veuillez envoyer une IMAGE (photo).")
        return WAITING_COVER
        
    try:
        if photo:
            # Prend la meilleure qualité
            file_id = photo[-1].file_id
        elif document and document.mime_type.startswith("image/"):
            file_id = document.file_id
        else:
            await update.message.reply_text("❌ Ce fichier n'est pas une image supportée.")
            return WAITING_COVER
            
        tg_file = await ctx.bot.get_file(file_id)
        
        # Download image into memory
        mem = BytesIO()
        await tg_file.download_to_memory(mem)
        img_bytes = mem.getvalue()
        
        ctx.user_data["tags"]["cover"] = img_bytes
        
        # Try to delete user message
        try: await update.message.delete()
        except Exception: pass
        
        tags = ctx.user_data["tags"]
        msg_id = ctx.user_data.get("msg_id")
        
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg_id,
            text=_get_menu_text(tags),
            reply_markup=_tag_kb(tags),
            parse_mode="Markdown"
        )
        return EDIT_MENU
        
    except Exception as e:
        log.error("Image error: %s", e)
        await update.message.reply_text("❌ Erreur lors du traitement de l'image.")
        return EDIT_MENU

def _cleanup(ctx):
    fp = ctx.user_data.get("edit_file")
    if fp and Path(fp).exists():
        try: Path(fp).unlink()
        except Exception: pass
    ctx.user_data.clear()

async def cmd_cancel_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: await update.message.delete()
    except: pass
    
    tags = ctx.user_data.get("tags")
    if tags:
        msg_id = ctx.user_data.get("msg_id")
        await ctx.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg_id,
            text=_get_menu_text(tags),
            reply_markup=_tag_kb(tags),
            parse_mode="Markdown"
        )
    return EDIT_MENU

async def cmd_cancel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _cleanup(ctx)
    await update.message.reply_text("❌ Édition annulée.")
    return ConversationHandler.END

# ── MAIN ───────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))

    edit_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.AUDIO, handle_audio_file),
            MessageHandler(filters.Document.MimeType("audio/mpeg") |
                           filters.Document.MimeType("audio/mp3"), handle_audio_file),
        ],
        states={
            EDIT_MENU: [
                CallbackQueryHandler(handle_btn),
            ],
            WAITING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
                CommandHandler("cancel", cmd_cancel_edit),
            ],
            WAITING_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_cover_input),
                CommandHandler("cancel", cmd_cancel_edit),
            ]
        },
        fallbacks=[CommandHandler("stop", cmd_cancel_all)],
        per_message=False,
    )
    
    app.add_handler(edit_conv)
    
    log.info("🎛 V3no Tag Editor UHQ — prêt.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
