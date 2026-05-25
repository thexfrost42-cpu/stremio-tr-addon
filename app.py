import asyncio
import logging
import os
import re
import unicodedata
import urllib.parse
from contextlib import asynccontextmanager
from typing import List, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Manifest ─────────────────────────────────────────────────────────────────
MANIFEST = {
    "id": "com.tr.turkce.addon.v12",
    "version": "1.12.4",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Türkçe Dublaj Film & Dizi — Dizipal, YabanciDizi, HDFilmCehennemi, FullHDFilmIzle",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ─── Domain Listeleri ─────────────────────────────────────────────────────────
DIZIPAL_DOMAINS = [
    "https://dizipal2073.com",
    "https://dizipal2066.com",
    "https://dizipal1548.com",
]

YABANCIDIZI_DOMAINS = [
    "https://yabancidizi.org",
    "https://yabancidizi.tv",
    "https://yabancidizi.net",
]

HDFILM_DOMAINS = [
    "https://www.hdfilmcehennemi.life",
    "https://www.hdfilmcehennemi.nl",
    "https://www.hdfilmcehennemi.net",
]

FULLHDFILM_DOMAINS = [
    "https://www.fullhdfilmizlesene.de",
    "https://www.fullhdfilmizlesene.com",
    "https://www.fullhdfilmizlesene.pw",
]

# ─── Config ───────────────────────────────────────────────────────────────────
TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "10"))

# ─── scrape.do Token Rotasyonu ────────────────────────────────────────────────
# Render Environment'a SCRAPE_DO_TOKEN_1 ... SCRAPE_DO_TOKEN_4 ekle
SCRAPER_API_KEYS: List[str] = [
    k for k in [
        os.getenv("SCRAPE_DO_TOKEN_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
        os.getenv("SCRAPE_DO_TOKEN_2", "ad94f3583fe44d0ca3635c5af37b73f2c64d90c0a59"),
        os.getenv("SCRAPE_DO_TOKEN_3", "c1cb4ed6152049b48307d7e570a485ef66ae1e3b703"),
        os.getenv("SCRAPE_DO_TOKEN_4", "14d6807a8de34c12840670630573ea6596f9036f277"),
    ]
    if k
]

_key_index: int = 0
_key_lock: Optional[asyncio.Lock] = None


def _get_key_lock() -> asyncio.Lock:
    global _key_lock
    if _key_lock is None:
        _key_lock = asyncio.Lock()
    return _key_lock


def _current_key() -> str:
    """Anlık aktif key'i döndürür (lock gerekmez, okuma atomik)."""
    return SCRAPER_API_KEYS[_key_index] if SCRAPER_API_KEYS else ""


async def _rotate_key() -> None:
    global _key_index
    async with _get_key_lock():
        _key_index = (_key_index + 1) % len(SCRAPER_API_KEYS)
    logger.warning(f"Key rotasyonu: {_key_index + 1}. key'e geçildi.")


# ─── HTTP Session ─────────────────────────────────────────────────────────────
_session: Optional[requests.AsyncSession] = None
_session_lock: Optional[asyncio.Lock] = None


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def get_session() -> requests.AsyncSession:
    global _session
    async with _get_session_lock():
        if _session is None:
            _session = requests.AsyncSession(impersonate="chrome120")
            logger.info("AsyncSession oluşturuldu.")
    return _session


# ─── Fetch ────────────────────────────────────────────────────────────────────
async def fetch_html(url: str) -> Optional[str]:
    """
    Hedef URL'yi scrape.do proxy'si üzerinden çeker.
    Format: https://api.scrape.do?token=TOKEN&url=ENCODED_URL
    429/403 alınca bir kez token rotasyonu yaparak tekrar dener.
    """
    if not SCRAPER_API_KEYS:
        logger.error("Hiç SCRAPE_DO_TOKEN tanımlanmamış!")
        return None

    s = await get_session()
    encoded = urllib.parse.quote(url, safe="")

    max_attempts = min(2, len(SCRAPER_API_KEYS))
    for attempt in range(max_attempts):
        token = _current_key()
        proxy_url = f"https://api.scrape.do?token={token}&url={encoded}"
        try:
            r = await s.get(proxy_url, timeout=TIMEOUT)
        except Exception as e:
            logger.error(f"fetch_html bağlantı hatası [{url}]: {e}")
            return None

        if r.status_code == 200:
            return r.text

        if r.status_code in (429, 403):
            logger.warning(f"HTTP {r.status_code} rate-limit (attempt {attempt + 1}) → token rotasyonu")
            await _rotate_key()
            continue

        logger.warning(f"HTTP {r.status_code} → {url}")
        return None

    return None


# ─── Cinemeta ─────────────────────────────────────────────────────────────────
async def get_media_name(imdb_id: str, video_type: str) -> str:
    """Cinemeta API'sinden film/dizi adını çeker. Proxy gerekmez."""
    pure_id = imdb_id.split(":")[0]
    try:
        s = await get_session()
        url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
        r = await s.get(url, timeout=8)
        if r.status_code == 200:
            name = r.json().get("meta", {}).get("name", "")
            if name:
                logger.info(f"Cinemeta adı: {name}")
                return name
    except Exception as e:
        logger.warning(f"Cinemeta hatası: {e}")
    return pure_id


# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────
def to_slug(text: str) -> str:
    """Metni URL-dostu slug formatına dönüştürür (TR karakter desteğiyle)."""
    tr_map = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisoucgisou")
    text = text.translate(tr_map)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text


def fix_src(src: str) -> str:
    """Protocol-relative URL'leri tam HTTPS URL'ye dönüştürür."""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    return src


def is_valid_src(src: str) -> bool:
    return bool(src and src.startswith(("http://", "https://", "//")))


def extract_dublaj_iframes(soup: BeautifulSoup) -> List[str]:
    """
    HTML'den Türkçe Dublaj iframe src'lerini çıkarır.

    Öncelik sırası:
      1. id veya class'ında 'dublaj' geçen kapsayıcılardaki iframe'ler
      2. 'türkçe dublaj' / 'turkce dublaj' metin etiketine yakın iframe'ler
      3. (Fallback) Sayfadaki tüm geçerli iframe'ler

    Fallback aktifleşirse akışlar yine "Türkçe Dublaj" etiketiyle döner
    çünkü scraping yapılan siteler zaten Türkçe içerik sunan platformlardır.
    """
    seen: set = set()
    iframes: List[str] = []

    def add(src: str) -> bool:
        fsrc = fix_src(src)
        if fsrc not in seen:
            seen.add(fsrc)
            iframes.append(fsrc)
            return True
        return False

    # ── Strateji 1: id/class içinde 'dublaj' geçen container ──────────────────
    containers = (
        soup.find_all(True, attrs={"id": re.compile(r"dublaj", re.I)})
        + soup.find_all(True, attrs={"class": re.compile(r"dublaj", re.I)})
    )
    for container in containers:
        for iframe in container.find_all("iframe"):
            src = iframe.get("src", "")
            if is_valid_src(src):
                add(src)

    if iframes:
        return iframes

    # ── Strateji 2: "türkçe dublaj" metin düğümüne yakın iframe ──────────────
    for text_node in soup.find_all(string=re.compile(r"t[uü]rk[cç]e\s*dublaj", re.I)):
        parent = text_node.parent
        for _ in range(7):  # en fazla 7 seviye yukarı çık
            if parent is None:
                break
            for iframe in parent.find_all("iframe"):
                src = iframe.get("src", "")
                if is_valid_src(src):
                    add(src)
            if iframes:
                break
            parent = parent.parent

    if iframes:
        return iframes

    # ── Strateji 3 (Fallback): tüm iframe'ler ────────────────────────────────
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if is_valid_src(src):
            add(src)

    return iframes


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 1 – DİZİPAL
# Dizi : {domain}/bolum/{slug}-{season}-sezon-{episode}-bolum/
# Film : {domain}/film/{slug}-izle/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_dizipal(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if not name:
        return []

    slug = to_slug(name)

    for domain in DIZIPAL_DOMAINS:
        # Tipe göre tek bir direkt URL kur, karıştırma
        if video_type == "series":
            if not (season and episode):
                return []
            url = f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum/"
        else:  # movie
            url = f"{domain}/film/{slug}-izle/"

        page_html = await fetch_html(url)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        srcs = extract_dublaj_iframes(soup)
        if srcs:
            streams = [
                {
                    "name": "TR Addon 🇹🇷",
                    "title": f"Dizipal #{i + 1} 📺\n🇹🇷 Türkçe Dublaj",
                    "externalUrl": src,
                }
                for i, src in enumerate(srcs)
            ]
            logger.info(f"Dizipal: {len(streams)} stream [{domain}]")
            return streams

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 2 – YABANCI DİZİ  (sadece dizi)
# Dizi : {domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_yabancidizi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    # Bu site sadece dizi içeriği sunar
    if video_type != "series" or not name or not season or not episode:
        return []

    slug = to_slug(name)

    for domain in YABANCIDIZI_DOMAINS:
        url = f"{domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/"
        page_html = await fetch_html(url)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        srcs = extract_dublaj_iframes(soup)
        if srcs:
            streams = [
                {
                    "name": "TR Addon 🇹🇷",
                    "title": f"YabanciDizi #{i + 1} 🚀\n🇹🇷 Türkçe Dublaj",
                    "externalUrl": src,
                }
                for i, src in enumerate(srcs)
            ]
            logger.info(f"YabanciDizi: {len(streams)} stream [{domain}]")
            return streams

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 3 – HD FİLM CEHENNEMİ  (hem dizi hem film)
# Dizi : {domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/
# Film : {domain}/film/{slug}-izle/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_hdfilmcehennemi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if not name:
        return []

    slug = to_slug(name)

    for domain in HDFILM_DOMAINS:
        # Tipe göre tek bir direkt URL kur, karıştırma
        if video_type == "series":
            if not (season and episode):
                return []
            url = f"{domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/"
        else:  # movie
            url = f"{domain}/film/{slug}-izle/"

        page_html = await fetch_html(url)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        srcs = extract_dublaj_iframes(soup)
        if srcs:
            streams = [
                {
                    "name": "TR Addon 🇹🇷",
                    "title": f"HDFilmCehennemi #{i + 1} 🔥\n🇹🇷 Türkçe Dublaj",
                    "externalUrl": src,
                }
                for i, src in enumerate(srcs)
            ]
            logger.info(f"HDFilmCehennemi: {len(streams)} stream [{domain}]")
            return streams

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 4 – FULLHD FİLM İZLE  (sadece film)
# Film : {domain}/{slug}-izle/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_fullhdfilmizle(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    # Bu site sadece film içeriği sunar
    if video_type != "movie" or not name:
        return []

    slug = to_slug(name)

    for domain in FULLHDFILM_DOMAINS:
        url = f"{domain}/{slug}-izle/"
        page_html = await fetch_html(url)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        srcs = extract_dublaj_iframes(soup)
        if srcs:
            streams = [
                {
                    "name": "TR Addon 🇹🇷",
                    "title": f"FullHDFilm #{i + 1} ⚡\n🇹🇷 Türkçe Dublaj",
                    "externalUrl": src,
                }
                for i, src in enumerate(srcs)
            ]
            logger.info(f"FullHDFilmIzle: {len(streams)} stream [{domain}]")
            return streams

    return []

        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        srcs = extract_dublaj_iframes(soup)
        if srcs:
            streams = [
                {
                    "name": "TR Addon 🇹🇷",
                    "title": f"FullHDFilm #{i + 1} ⚡\n🇹🇷 Türkçe Dublaj",
                    "externalUrl": src,
                }
                for i, src in enumerate(srcs)
            ]
            logger.info(f"FullHDFilmIzle: {len(streams)} stream [{domain}]")
            return streams

    return []


# ─────────────────────────────────────────────────────────────────────────────
# APP LIFECYCLE & ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    # Startup
    await get_session()
    if not SCRAPER_API_KEYS:
        logger.warning("Hiç SCRAPE_DO_TOKEN tanımlanmamış! Render env değişkenlerini kontrol et.")
    else:
        logger.info(f"scrape.do aktif. {len(SCRAPER_API_KEYS)} token yüklendi.")
    logger.info("=== TR Sinema Paketi başlatıldı ===")
    yield
    # Shutdown (temizlik gerekmez)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scrape_do": (
            f"{len(SCRAPER_API_KEYS)} token aktif"
            if SCRAPER_API_KEYS
            else "TANIMLANMAMIŞ"
        ),
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    """
    Stremio stream endpoint.

    imdb_id formatı:
      Film  : tt1234567
      Dizi  : tt1234567:1:2  → sezon=1, bölüm=2

    Scraper'lar sırayla çalışır. İlk sonuç döner, diğerleri çağrılmaz.

    Film  pipeline: HDFilmCehennemi → FullHDFilmIzle → Dizipal
    Dizi  pipeline: HDFilmCehennemi → Dizipal → YabanciDizi
    """
    parts = imdb_id.split(":")
    season: Optional[str] = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode: Optional[str] = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(f"İstek → type={video_type} id={imdb_id} s={season} e={episode}")

    try:
        name = await get_media_name(imdb_id, video_type)

        if video_type == "movie":
            # YabanciDizi sadece dizi → pipeline'a dahil değil
            pipeline = [
                ("HDFilmCehennemi", scrape_hdfilmcehennemi),
                ("FullHDFilmIzle",  scrape_fullhdfilmizle),
                ("Dizipal",         scrape_dizipal),
            ]
        else:  # series
            # FullHDFilmIzle sadece film → pipeline'a dahil değil
            pipeline = [
                ("HDFilmCehennemi", scrape_hdfilmcehennemi),
                ("Dizipal",         scrape_dizipal),
                ("YabanciDizi",     scrape_yabancidizi),
            ]

        for source_name, scraper_fn in pipeline:
            try:
                streams = await scraper_fn(name, video_type, season, episode)
            except Exception as e:
                logger.error(f"{source_name} scraper hatası: {e}")
                streams = []

            if streams:
                logger.info(f"✅ {source_name} → {len(streams)} stream bulundu.")
                return {"streams": streams}

            logger.info(f"⬜ {source_name} → sonuç yok, sonraki kaynağa geçiliyor.")

    except Exception as e:
        logger.error(f"get_stream genel hatası: {e}")

    logger.info("Hiçbir kaynaktan stream döndürülemedi.")
    return {"streams": []}
