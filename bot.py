"""
🎵 MP3 BOT — Direct Deezer CDN · YouTube · Spotify · Metadata editor
"""

import os, re, json, hashlib, asyncio, logging, tempfile, time
import urllib.request, urllib.parse, http.cookiejar
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from Crypto.Cipher import Blowfish, AES
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, TDRC, ID3NoHeaderError
from mutagen.mp3 import MP3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters,
)

# ── CONFIG ─────────────────────────────────────────────────────────
BOT_TOKEN   = "8618131684:AAGQmyd-F-5TcDilO4lZUu_WH3HQVxhYyXw"
TMPDIR      = Path(tempfile.gettempdir()) / "mp3bot"
TMPDIR.mkdir(exist_ok=True)
MAX_WORKERS = 16

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# Injecte ffmpeg dans le PATH si pas trouvé (Railway, Docker, etc.)
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    log.info("ffmpeg injecté via static-ffmpeg")
except Exception as _e:
    log.warning("static-ffmpeg non dispo: %s", _e)

# ── DEEZER CRYPTO ──────────────────────────────────────────────────
_GW_API    = "https://www.deezer.com/ajax/gw-light.php"
_MEDIA_API = "https://media.deezer.com/v1/get_url"
_URL_KEY   = b"jo6aey6haid2Teih"
_BF_SECRET = "g4el58wc0zvf9na1"
_SEP       = b"\xa4"

_QUALITY_CHAIN = [
    ("FLAC",    9, "FILESIZE_FLAC"),
    ("MP3_320", 3, "FILESIZE_MP3_320"),
    ("MP3_128", 1, "FILESIZE_MP3_128"),
]

def _bf_key(sng_id: str) -> bytes:
    h = hashlib.md5(str(sng_id).encode()).hexdigest()
    return bytes(ord(h[i]) ^ ord(h[i+16]) ^ ord(_BF_SECRET[i]) for i in range(16))

def _cdn_url(md5: str, quality: int, sng_id: str, version: str) -> str:
    step1 = _SEP.join([md5.encode(), str(quality).encode(), sng_id.encode(), version.encode()])
    md5v  = hashlib.md5(step1).hexdigest().encode()
    step2 = md5v + _SEP + step1 + _SEP
    while len(step2) % 16: step2 += b"."
    enc = AES.new(_URL_KEY, AES.MODE_ECB).encrypt(step2)
    return f"https://e-cdns-proxy-{md5[0]}.dzcdn.net/mobile/1/{enc.hex()}"

def _decrypt_stream(url: str, sng_id: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        key = _bf_key(sng_id)
        iv  = bytes(range(8))
        buf, idx = BytesIO(), 0
        while True:
            chunk = resp.read(2048)
            if not chunk: break
            if idx % 3 == 0 and len(chunk) == 2048:
                chunk = Blowfish.new(key, Blowfish.MODE_CBC, iv).decrypt(chunk)
            buf.write(chunk)
            idx += 1
    return buf.getvalue()

# ── DEEZER SESSION ─────────────────────────────────────────────────
def _get_arl() -> str:
    for p in [
        os.path.join(os.path.expandvars(r"%APPDATA%"), "deezer_ripper", ".arl"),
        os.path.join(os.path.dirname(__file__), "deemix_config", ".arl"),
    ]:
        if os.path.exists(p):
            v = open(p, encoding="utf-8").read().strip()
            if v: return v
    cred = os.path.join(os.path.expandvars(r"%APPDATA%"), "deemix", ".credentials")
    if os.path.exists(cred):
        try:
            v = json.load(open(cred)).get("arl", "").strip()
            if v: return v
        except Exception: pass
    return "51f67d6819e3dace58aa0a84d8e4f036d3a94bb31bd7c578032011a9360c7f4b5abd1032b93e4f94d42a6e81f320e45dd5957b18d6fee3712119da5057ca3e602e6ea8661ffbb9eb393b0d14bb982902ca760da8474995a453abcf6badfc03a1"

def _save_arl(arl: str):
    for p in [
        os.path.join(os.path.expandvars(r"%APPDATA%"), "deezer_ripper", ".arl"),
        os.path.join(os.path.dirname(__file__), "deemix_config", ".arl"),
    ]:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try: open(p, "w", encoding="utf-8").write(arl)
        except Exception: pass

class DeezerSession:
    def __init__(self, arl: str):
        self.arl = arl
        self.token = ""
        self.license_token = ""
        self.user = "?"
        self.plan = "Free"
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cj))
        self._cj.set_cookie(http.cookiejar.Cookie(
            0, "arl", arl, None, False, ".deezer.com", True, True,
            "/", True, True, None, True, None, None, {}, False,
        ))

    def _gw(self, method: str, params: dict = None, retry=True) -> dict:
        url  = f"{_GW_API}?method={method}&input=3&api_version=1.0&api_token={self.token}"
        body = json.dumps(params or {}).encode()
        req  = urllib.request.Request(url, data=body, headers={
            "User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
            "Origin": "https://www.deezer.com", "Referer": "https://www.deezer.com/",
        })
        with self._opener.open(req, timeout=15) as r:
            data = json.loads(r.read())
            
        if retry and (data.get("error") or not data.get("results")):
            log.warning("Deezer GW error or empty results, refreshing token...")
            if self.auth():
                return self._gw(method, params, retry=False)
                
        return data

    def auth(self) -> bool:
        try:
            d = self._gw("deezer.getUserData", retry=False)["results"]
            u = d.get("USER", {})
            if not u.get("USER_ID"): return False
            self.token         = d.get("checkForm", "")
            self.license_token = u.get("OPTIONS", {}).get("license_token", "")
            self.user          = u.get("BLOG_NAME", "?")
            sq = u.get("OPTIONS", {}).get("web_sound_quality", {})
            self.plan = "HiFi" if sq.get("lossless") else "Premium" if sq.get("high") else "Free"
            return True
        except Exception as e:
            log.error("Auth: %s", e)
            return False

    def track_info(self, sng_id) -> dict:
        return self._gw("song.getData", {"sng_id": str(sng_id)}).get("results", {})

    def album_tracks(self, alb_id) -> list:
        return self._gw("song.getListByAlbum", {"alb_id": int(alb_id), "nb": 500}).get("results", {}).get("data", [])

    def playlist_tracks(self, pl_id) -> list:
        return self._gw("playlist.getSongs", {"playlist_id": int(pl_id), "nb": 2000}).get("results", {}).get("data", [])

    def get_url(self, track: dict, quality_name: str, quality_code: int) -> str | None:
        token = track.get("TRACK_TOKEN")
        if self.license_token and token:
            try:
                body = json.dumps({
                    "license_token": self.license_token,
                    "media": [{"type": "FULL", "formats": [{"cipher": "BF_CBC_STRIPE", "format": quality_name}]}],
                    "track_tokens": [token],
                }).encode()
                req = urllib.request.Request(_MEDIA_API, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read())
                    srcs = d.get("data", [{}])[0].get("media", [{}])[0].get("sources", [])
                    if srcs: return srcs[0]["url"]
            except Exception: pass
        md5 = track.get("MD5_ORIGIN", "")
        ver = track.get("MEDIA_VERSION", "")
        sid = track.get("SNG_ID", "")
        if md5 and ver and sid:
            return _cdn_url(md5, quality_code, sid, ver)
        return None

_SESSION: DeezerSession | None = None

def get_session() -> DeezerSession | None:
    global _SESSION
    if _SESSION is None:
        s = DeezerSession(_get_arl())
        if s.auth():
            _SESSION = s
            log.info("Deezer: %s (%s)", s.user, s.plan)
    return _SESSION

# ── COVER ART ──────────────────────────────────────────────────────
_cover_cache: dict[str, bytes] = {}

def get_cover(md5_img: str, size: int = 500) -> bytes | None:
    if not md5_img: return None
    key = f"{md5_img}_{size}"
    if key in _cover_cache: return _cover_cache[key]
    url = f"https://e-cdns-images.dzcdn.net/images/cover/{md5_img}/{size}x{size}-000000-80-0-0.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read()
        _cover_cache[key] = data
        return data
    except Exception: return None

# ── TAGGING ────────────────────────────────────────────────────────
def tag_mp3(path: Path, title="", artist="", album="", track_num="", year="", cover: bytes = None):
    try:
        audio = MP3(str(path), ID3=ID3)
        try: audio.add_tags()
        except Exception: pass
        T = audio.tags
        if title:     T.add(TIT2(encoding=3, text=[title]))
        if artist:    T.add(TPE1(encoding=3, text=[artist]))
        if album:     T.add(TALB(encoding=3, text=[album]))
        if track_num: T.add(TRCK(encoding=3, text=[str(track_num)]))
        if year:      T.add(TDRC(encoding=3, text=[str(year)[:4]]))
        if cover:
            T.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=cover))
        audio.save(v2_version=3)
    except Exception as e:
        log.warning("Tag error: %s", e)

def read_tags(path: Path) -> dict:
    try: T = ID3(str(path))
    except ID3NoHeaderError: T = ID3()
    def s(k): return str(T.get(k, "")) or ""
    return {"title": s("TIT2") or path.stem, "artist": s("TPE1"), "album": s("TALB")}

def write_tags(path: Path, title="", artist="", album=""):
    try: T = ID3(str(path))
    except ID3NoHeaderError: T = ID3()
    if title:  T["TIT2"] = TIT2(encoding=3, text=title)
    if artist: T["TPE1"] = TPE1(encoding=3, text=artist)
    if album:  T["TALB"] = TALB(encoding=3, text=album)
    T.save(str(path), v2_version=3)

# ── DEEZER TRACK DOWNLOADER ────────────────────────────────────────
_safename_re = re.compile(r'[<>:"/\\|?*]')
def _safe(s: str) -> str:
    return _safename_re.sub("_", s).strip()[:120]

def _resolve_artist(track: dict) -> str:
    """
    Récupère tous les artistes principaux dans l'ordre affiché par Deezer.
    Priorité: ARTISTS (ROLE_ID=0 trié) → SNG_CONTRIBUTORS.main_artist → ART_NAME
    """
    artists_list = track.get("ARTISTS") or []
    if isinstance(artists_list, list) and artists_list:
        mains = sorted(
            [a for a in artists_list if str(a.get("ROLE_ID", "0")) == "0"],
            key=lambda a: int(a.get("ARTISTS_SONGS_ORDER") or 99)
        )
        names = [a["ART_NAME"] for a in mains if a.get("ART_NAME")]
        if not names:
            names = [a["ART_NAME"] for a in artists_list if a.get("ART_NAME")]
        if names: return " & ".join(names)
    contrib = track.get("SNG_CONTRIBUTORS") or {}
    mains   = contrib.get("main_artist", [])
    if mains: return " & ".join(mains)
    return track.get("ART_NAME", "?")

def deezer_dl_track(session: DeezerSession, track: dict,
                    out_dir: Path, cover: bytes | None) -> Path | None:
    sng_id  = track.get("SNG_ID", "")
    title   = track.get("SNG_TITLE", "?")
    artist  = _resolve_artist(track)
    album   = track.get("ALB_TITLE", "")
    tnum    = track.get("TRACK_NUMBER", "")
    year    = track.get("PHYSICAL_RELEASE_DATE", "")[:4]
    alb_pic = track.get("ALB_PICTURE", "")

    url = None
    for qname, qcode, qsize_key in _QUALITY_CHAIN:
        if int(track.get(qsize_key, 0) or 0) > 0:
            url = session.get_url(track, qname, qcode)
            if url: break

    if not url:
        log.warning("No URL: %s", title)
        return None

    try:
        data = _decrypt_stream(url, sng_id)
    except Exception as e:
        log.error("Decrypt [%s]: %s", title, e)
        return None

    if len(data) < 1024:
        return None

    out_path = out_dir / f"{_safe(artist)} - {_safe(title)}.mp3"
    out_path.write_bytes(data)
    art = cover or get_cover(alb_pic)
    tag_mp3(out_path, title=title, artist=artist, album=album,
            track_num=tnum, year=year, cover=art)
    return out_path

# ── URL DETECTION ──────────────────────────────────────────────────
_YT_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+", re.I)
_SP_RE = re.compile(r"(https?://)?(open\.)?spotify\.com/(track|album|playlist)/.+", re.I)
_DZ_RE = re.compile(r"deezer\.com/(?:\w{2}/)?(track|album|playlist)/(\d+)", re.I)
_SC_RE = re.compile(r"(https?://)?(www\.)?soundcloud\.com/.+", re.I)

def detect(text: str) -> tuple[str, str] | None:
    t = text.strip()
    if _YT_RE.match(t): return "youtube", t
    if _SP_RE.match(t): return "spotify", t
    if _SC_RE.match(t): return "soundcloud", t
    if re.search(r"deezer\.com", t, re.I): return "deezer", t
    return None

def parse_deezer(url: str) -> tuple[str, str] | None:
    m = _DZ_RE.search(url)
    return (m.group(1), m.group(2)) if m else None

# ── DOWNLOADERS ────────────────────────────────────────────────────
async def yt_download(url: str, out_dir: Path) -> list[Path]:
    import uuid as _uuid
    # Dossier unique par téléchargement → glob simple et sans conflit
    work = out_dir / _uuid.uuid4().hex[:8]
    work.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", url,
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--no-playlist", "--embed-thumbnail", "--add-metadata",
        "--no-check-certificates", "--geo-bypass",
        "-o", str(work / "%(title)s.%(ext)s"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if stderr:
        log.warning("yt-dlp: %s", stderr.decode("utf-8", errors="replace")[-600:])

    files = list(work.glob("*.mp3"))
    if not files:
        raise RuntimeError("yt-dlp: aucun MP3 produit.")
    return files

async def sp_download(url: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "spotdl", "download", url,
        "--output", str(out_dir / "{title} - {artists}.{output-ext}"),
        "--format", "mp3", "--threads", "4",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(out_dir),
    )
    await proc.communicate()
    files = sorted(out_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    if not files: raise RuntimeError("spotdl: aucun MP3 produit.")
    return files

# ── TELEGRAM SEND ──────────────────────────────────────────────────
async def send_mp3(update: Update, path: Path):
    if not path.exists(): return
    if path.stat().st_size > 50 * 1024 * 1024:
        await update.message.reply_text(f"⚠️ `{path.name}` >50 Mo", parse_mode="Markdown")
        return
    thumb = None
    try:
        audio = MP3(str(path), ID3=ID3)
        title  = str(audio.tags.get("TIT2", path.stem))
        artist = str(audio.tags.get("TPE1", ""))
        album  = str(audio.tags.get("TALB", ""))
        for k, v in audio.tags.items():
            if k.startswith("APIC"): thumb = v.data; break
    except Exception:
        title, artist, album = path.stem, "", ""

    caption = f"🎶 *{title}*"
    if artist: caption += f"\n🎤 {artist}"
    if album:  caption += f"\n💿 {album}"

    with open(path, "rb") as f:
        kw = dict(audio=f, caption=caption, parse_mode="Markdown",
                  title=title, performer=artist,
                  read_timeout=60, write_timeout=60)
        if thumb: kw["thumbnail"] = BytesIO(thumb)
        await update.message.reply_audio(**kw)

# ── TELEGRAM HANDLERS ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session()
    dz = f"✅ Deezer CDN ({s.user} · {s.plan})" if s else "❌ Deezer off — `/set_arl <arl>`"
    await update.message.reply_text(
        "🎵 *MP3 Bot v6*\n\n"
        "• Lien **Deezer / Spotify / YouTube / SoundCloud** → MP3 + cover\n"
        "• Fichier **.mp3** → éditeur de tags\n\n"
        "💡 Tape /help pour voir toutes les commandes.\n\n" + dz,
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 *Commandes Dispos :*\n\n"
        "🔗 *Envoyer un lien* : Spotify, Deezer, YouTube ou SoundCloud pour un téléchargement immédiat.\n"
        "🎵 *Envoyer un fichier mp3* : Ouvre l'éditeur de métadonnées intégré.\n"
        "🔑 `/set_arl <ton_arl>` : Connecte ton compte Deezer pour la qualité max.\n"
        "🔎 `/search <artiste ou titre>` : (bientôt) Chercher et télécharger directement depuis Telegram.\n"
        "🛑 `/cancel` : Annule l'édition en cours.\n",
        parse_mode="Markdown",
    )

async def cmd_set_arl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _SESSION
    if not ctx.args:
        await update.message.reply_text("Usage : `/set_arl <ton_arl>`", parse_mode="Markdown")
        return
    arl = ctx.args[0].strip()
    _save_arl(arl)
    _SESSION = None
    s = get_session()
    if s:
        await update.message.reply_text(f"✅ *{s.user}* ({s.plan})", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ ARL invalide ou expiré.")

async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    result = detect(text)
    if not result: return

    platform, url = result
    chat_id = update.effective_chat.id
    out_dir = TMPDIR / str(chat_id)
    out_dir.mkdir(exist_ok=True)
    for f in out_dir.rglob("*.mp3"):
        try: f.unlink()
        except Exception: pass

    msg = await update.message.reply_text(f"⏳ *{platform.upper()}*...", parse_mode="Markdown")

    try:
        if platform in ("youtube", "soundcloud"):
            files = await yt_download(url, out_dir)
            try: await msg.delete()
            except Exception: pass
            for f in files:
                await send_mp3(update, f)

        elif platform in ("deezer", "spotify"):
            session = get_session()
            if not session:
                await msg.edit_text("❌ Deezer indispo — `/set_arl <arl>`", parse_mode="Markdown")
                return

            loop = asyncio.get_running_loop()
            tracks = []

            if platform == "spotify":
                import subprocess
                mfile = out_dir / "meta.spotdl"
                proc = await asyncio.create_subprocess_exec(
                    "spotdl", "save", url, "--save-file", str(mfile),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await proc.communicate()
                if not mfile.exists():
                    await msg.edit_text("❌ Erreur metadata Spotify.")
                    return
                try:
                    sdata = json.loads(mfile.read_text("utf-8"))
                    
                    def _search(query):
                        try:
                            r = urllib.request.urlopen(f"https://api.deezer.com/search?q={urllib.parse.quote(query)}&limit=1").read()
                            return json.loads(r).get("data", [])
                        except Exception: return []

                    async def _find_on_deezer(t):
                        q = f"{t.get('name', '')} {t.get('artist', '')}".strip()
                        if not q: return None
                        res = await loop.run_in_executor(None, _search, q)
                        if res:
                            return await loop.run_in_executor(None, session.track_info, res[0]["id"])
                        return None

                    # Recherche concurrentielle pour toutes les pistes Spotify
                    found = await asyncio.gather(*[_find_on_deezer(t) for t in sdata])
                    tracks = [t for t in found if t]
                    
                except Exception as e:
                    log.error("Spotify meta parse: %s", e)

            else:
                # ── DEEZER NORMAL ──
                ptype = parse_deezer(url)
                if not ptype:
                    await msg.edit_text("❌ Lien invalide.")
                    return
                kind, obj_id = ptype

                if kind == "track":
                    raw    = await loop.run_in_executor(None, session.track_info, obj_id)
                    tracks = [raw] if raw else []
                elif kind == "album":
                    tracks = await loop.run_in_executor(None, session.album_tracks, obj_id)
                elif kind == "playlist":
                    tracks = await loop.run_in_executor(None, session.playlist_tracks, obj_id)
                else:
                    tracks = []

            if not tracks:
                await msg.edit_text("❌ Aucune piste.")
                return

            n = len(tracks)

            # ── Télécharger la cover + toutes les pistes en PARALLÈLE ──
            # La cover est partagée entre les workers via cover_holder[]
            alb_pic      = tracks[0].get("ALB_PICTURE", "")
            cover_holder = [None]       # mutable pour accès thread-safe (GIL suffit)
            cover_ready  = asyncio.Event()

            async def fetch_cover_async():
                cover_holder[0] = await loop.run_in_executor(None, get_cover, alb_pic)
                cover_ready.set()

            cover_task = asyncio.create_task(fetch_cover_async())

            queue: asyncio.Queue[Path | None] = asyncio.Queue()

            def worker(t):
                # Attendre max 1.5s que la cover soit dispo, ensuite on fonce sans
                deadline = time.monotonic() + 1.5
                while cover_holder[0] is None and time.monotonic() < deadline:
                    time.sleep(0.03)
                try:
                    p = deezer_dl_track(session, t, out_dir, cover_holder[0])
                    loop.call_soon_threadsafe(queue.put_nowait, p)
                except Exception as e:
                    log.error("Worker: %s", e)
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            ex = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, n))
            for t in tracks:
                ex.submit(worker, t)

            # Supprimer le spinner dès la 1ère piste prête
            msg_deleted = False
            sent = 0
            for _ in range(n):
                path = await queue.get()
                if not msg_deleted:
                    try: await msg.delete()
                    except Exception: pass
                    msg_deleted = True
                if path and path.exists():
                    await send_mp3(update, path)
                    sent += 1

            await cover_task
            ex.shutdown(wait=False)
            if not msg_deleted:
                try: await msg.delete()
                except Exception: pass

            if sent == 0:
                await update.message.reply_text("❌ Aucune piste téléchargée.")

    except Exception as e:
        log.exception("handle_link")
        try: await msg.edit_text(f"❌ `{str(e)[:250]}`", parse_mode="Markdown")
        except Exception: pass

# ── METADATA EDITOR ────────────────────────────────────────────────
EDIT = 0

def _tag_kb(tags: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 {tags['title'][:38]}", callback_data="e_title")],
        [InlineKeyboardButton(f"🎤 {tags['artist'][:38] or '—'}", callback_data="e_artist")],
        [InlineKeyboardButton(f"💿 {tags['album'][:38] or '—'}", callback_data="e_album")],
        [InlineKeyboardButton("✅ Valider", callback_data="e_ok"),
         InlineKeyboardButton("❌ Annuler", callback_data="e_cancel")],
    ])

async def handle_audio_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio or update.message.document
    if not audio: return ConversationHandler.END
    fname = getattr(audio, "file_name", None) or "track.mp3"
    if not (fname.lower().endswith(".mp3") or
            getattr(audio, "mime_type", "") in ("audio/mpeg", "audio/mp3")):
        await update.message.reply_text("Envoie un *.mp3*", parse_mode="Markdown")
        return ConversationHandler.END

    edt_dir = TMPDIR / str(update.effective_chat.id) / "edit"
    edt_dir.mkdir(parents=True, exist_ok=True)
    msg = await update.message.reply_text("⏳...")
    tg    = await audio.get_file()
    local = edt_dir / fname
    await tg.download_to_drive(str(local))
    await msg.delete()

    ctx.user_data.update(edit_file=str(local), edit_field=None, tags=read_tags(local))
    tags = ctx.user_data["tags"]
    await update.message.reply_text(
        f"📋 *Tags :*\n📝 `{tags['title']}`\n🎤 `{tags['artist']}`\n💿 `{tags['album']}`",
        reply_markup=_tag_kb(tags), parse_mode="Markdown",
    )
    return EDIT

async def handle_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data in ("e_title", "e_artist", "e_album"):
        ctx.user_data["edit_field"] = data[2:]
        labels = {"title": "titre", "artist": "artiste", "album": "album"}
        await q.message.reply_text(f"Envoie le nouveau **{labels[data[2:]]}** :", parse_mode="Markdown")
        return EDIT

    if data == "e_cancel":
        _cleanup(ctx)
        await q.message.reply_text("❌ Annulé.")
        return ConversationHandler.END

    if data == "e_ok":
        fp = Path(ctx.user_data.get("edit_file", ""))
        if not fp.exists():
            await q.message.reply_text("❌ Fichier introuvable.")
            return ConversationHandler.END
        tags = ctx.user_data["tags"]
        write_tags(fp, **tags)
        thumb = None
        try:
            T = MP3(str(fp), ID3=ID3).tags
            for k, v in T.items():
                if k.startswith("APIC"): thumb = v.data; break
        except Exception: pass
        with open(fp, "rb") as f:
            kw = dict(audio=f,
                      caption=f"✅ `{tags['title']}` — `{tags['artist']}`",
                      parse_mode="Markdown",
                      title=tags["title"], performer=tags["artist"],
                      read_timeout=60, write_timeout=60)
            if thumb: kw["thumbnail"] = BytesIO(thumb)
            await q.message.reply_audio(**kw)
        _cleanup(ctx)
        return ConversationHandler.END

    return EDIT

async def handle_text_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    field = ctx.user_data.get("edit_field")
    if not field:
        await update.message.reply_text("Clique sur un champ d'abord ☝️")
        return EDIT
    val = update.message.text.strip()
    ctx.user_data["tags"][field] = val
    ctx.user_data["edit_field"]  = None
    tags = ctx.user_data["tags"]
    await update.message.reply_text(
        f"👍 `{val}`\n\n📝 `{tags['title']}`\n🎤 `{tags['artist']}`\n💿 `{tags['album']}`",
        reply_markup=_tag_kb(tags), parse_mode="Markdown",
    )
    return EDIT

def _cleanup(ctx):
    fp = ctx.user_data.get("edit_file")
    if fp and Path(fp).exists():
        try: Path(fp).unlink()
        except Exception: pass
    ctx.user_data.clear()

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _cleanup(ctx)
    await update.message.reply_text("❌ Annulé.")
    return ConversationHandler.END

# ── MAIN ───────────────────────────────────────────────────────────
def main():
    import threading
    threading.Thread(target=get_session, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("set_arl", cmd_set_arl))

    edit_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.AUDIO, handle_audio_file),
            MessageHandler(filters.Document.MimeType("audio/mpeg") |
                           filters.Document.MimeType("audio/mp3"), handle_audio_file),
        ],
        states={EDIT: [
            CallbackQueryHandler(handle_btn),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_edit),
        ]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )
    app.add_handler(edit_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    log.info("🎵 MP3 Bot — prêt.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
