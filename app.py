import asyncio
import logging
import os
import re
import unicodedata
import urllib.parse
from typing import Optional, List

from bs4 import BeautifulSoup
from curl_cffi import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─────────────────────────────────────────────────────────
# LOGLAMA
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# FASTAPI
# ─────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────
MANIFEST = {
    "id": "com.tr.turkce.addon.v10",
    "version": "10.0.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Türkçe Dublaj & Altyazılı Film/Dizi — Dizipal, YabanciDizi, HDFilmCehennemi",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ─────────────────────────────────────────────────────────
# DOMAIN LİSTELERİ  (baştaki çalışmazsa sıradakini dene)
# ─────────────────────────────────────────────────────────
DIZIPAL_DOMAINS = [
    "https://dizipal2073.com",
    "https://dizipal2066.com",
    "https://dizipal2074.com",
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

# ─────────────────────────────────────────────────────────
# AYARLAR
# ─────────────────────────────────────────────────────────
TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "15"))

# ─────────────────────────────────────────────────────────
# GLOBAL SESSION  (thread-safe)
# ─────────────────────────────────────────────────────────
_session: Optional[requests.AsyncSession] = None
_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def get_session() -> requests.AsyncSession:
    global _session
    async with _get_lock():
        if _session is None:
            _session = requests.AsyncSession(impersonate="chrome120")
            logger.info("AsyncSession olusturuldu.")
    return _session


# ─────────────────────────────────────────────────────────
# YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────────────────────

async def fetch_html(url: str) -> Optional[str]:
    """Verilen URL'yi çek, HTML döndür."""
    try:
        s = await get_session()
        r = await s.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
        logger.warning(f"HTTP {r.status_code} → {url}")
    except Exception as e:
        logger.error(f"fetch_html hatasi [{url}]: {e}")
    return None


async def get_media_name(imdb_id: str, video_type: str) -> str:
    """Cinemeta API'sinden film/dizi adını al."""
    pure_id = imdb_id.split(":")[0]
    try:
        s = await get_session()
        url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
        r = await s.get(url, timeout=8)
        if r.status_code == 200:
            name = r.json().get("meta", {}).get("name", "")
            if name:
                logger.info(f"Cinemeta adi: {name}")
                return name
    except Exception as e:
        logger.warning(f"Cinemeta hatasi: {e}")
    return pure_id


def to_slug(text: str) -> str:
    """
    Film/dizi adını Türk sitelerinin beklediği slug formatına çevir.
    Örn: "Game of Thrones" → "game-of-thrones"
         "Ölüm Treni"     → "olum-treni"
    """
    # Unicode normalize → ASCII'ye yaklaştır
    text = unicodedata.normalize("NFKD", text)
    # Türkçe karakterleri manuel değiştir
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")
    text = text.translate(tr_map)
    # Küçük harf
    text = text.lower()
    # Sadece harf, rakam, boşluk bırak
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    # Boşlukları tire yap
    text = re.sub(r"\s+", "-", text.strip())
    # Çift tireyi tek yap
    text = re.sub(r"-+", "-", text)
    return text


def fix_src(src: str) -> str:
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    return src


def is_valid_src(src: str) -> bool:
    return bool(src and src.startswith(("http://", "https://", "//")))


# ─────────────────────────────────────────────────────────
# SCRAPER 1: DİZİPAL
# URL yapısı:
#   Dizi sayfası : /dizi/<slug>
#   Bölüm sayfası: /bolum/<slug>-<s>-sezon-<e>-bolum
#   Film sayfası : /film/<slug>-izle   VEYA  arama → /filmler/<slug>
# ─────────────────────────────────────────────────────────
async def scrape_dizipal(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    streams = []
    if not name:
        return streams

    slug = to_slug(name)
    safe_name = urllib.parse.quote_plus(name)

    for domain in DIZIPAL_DOMAINS:
        # Önce direkt URL dene (daha hızlı)
        if video_type == "series" and season and episode:
            target = f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum"
        else:
            target = f"{domain}/film/{slug}-izle"

        page_html = await fetch_html(target)

        # Direkt URL bulunamazsa arama yap
        if not page_html or "404" in (page_html[:500] if page_html else ""):
            search_url = f"{domain}/?s={safe_name}"
            search_html = await fetch_html(search_url)
            if not search_html:
                continue
            soup = BeautifulSoup(search_html, "lxml")
            # İlk sonucu al
            pattern = r"/bolum/|/film/|/dizi/"
            link = soup.find("a", href=re.compile(pattern))
            if not link:
                continue
            href = link["href"]
            if not href.startswith("http"):
                href = f"{domain}{href}"
            # Dizi bölümüne yönlendir
            if video_type == "series" and season and episode:
                # Dizi sayfasını bul, bolum URL'si oluştur
                href = f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum"
            page_html = await fetch_html(href)

        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        iframes = soup.find_all("iframe")
        found = []
        for i, iframe in enumerate(iframes):
            src = iframe.get("src", "")
            if is_valid_src(src):
                found.append({
                    "name": "TR Addon 🇹🇷",
                    "title": f"Dizipal #{i+1} 📺",
                    "externalUrl": fix_src(src),
                })

        if found:
            streams.extend(found)
            logger.info(f"Dizipal: {len(found)} stream [{domain}]")
            break  # Çalışan domain bulundu

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 2: YABANCI DİZİ
# URL yapısı:
#   Arama       : /arama?q=<isim>
#   Dizi sayfası: /dizi/<slug>
#   Bölüm       : /dizi/<slug>/sezon-<s>/bolum-<e>
# ─────────────────────────────────────────────────────────
async def scrape_yabancidizi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    streams = []
    if not name:
        return streams

    safe_name = urllib.parse.quote_plus(name)

    for domain in YABANCIDIZI_DOMAINS:
        search_html = await fetch_html(f"{domain}/arama?q={safe_name}")
        if not search_html:
            continue

        soup = BeautifulSoup(search_html, "lxml")
        link = soup.find("a", href=re.compile(r"/dizi/|/film/"))
        if not link:
            continue

        href = link["href"]
        if not href.startswith("http"):
            href = f"{domain}{href}"

        # Bölüm URL'si ekle
        if video_type == "series" and season and episode:
            href = f"{href.rstrip('/')}/sezon-{season}/bolum-{episode}"

        page_html = await fetch_html(href)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        found = []
        for i, iframe in enumerate(soup.find_all("iframe")):
            src = iframe.get("src", "")
            if is_valid_src(src):
                found.append({
                    "name": "TR Addon 🇹🇷",
                    "title": f"YabanciDizi #{i+1} 🚀",
                    "externalUrl": fix_src(src),
                })

        if found:
            streams.extend(found)
            logger.info(f"YabanciDizi: {len(found)} stream [{domain}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 3: HD FİLM CEHENNEMİ
# URL yapısı:
#   Arama: /search/<isim>
#   Dizi : /dizi/<slug>/<s>-sezon-<e>-bolum/  (tahmin)
#   Film : /film/<slug>/
# ─────────────────────────────────────────────────────────
async def scrape_hdfilmcehennemi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    streams = []
    if not name:
        return streams

    safe_name = urllib.parse.quote_plus(name)

    for domain in HDFILM_DOMAINS:
        search_html = await fetch_html(f"{domain}/search/{safe_name}")
        if not search_html:
            continue

        soup = BeautifulSoup(search_html, "lxml")
        link = soup.find("a", href=re.compile(r"/film/|/dizi/"))
        if not link:
            continue

        href = link["href"]
        if not href.startswith("http"):
            href = f"{domain}{href}"

        # Dizi bölümüne yönlendir
        if video_type == "series" and season and episode:
            href = f"{href.rstrip('/')}/{season}-sezon-{episode}-bolum/"

        page_html = await fetch_html(href)
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "lxml")
        player_pattern = re.compile(
            r"vidmoly|moly|player|odnoklassniki|ok\.ru|hdmoly|vk\.com"
        )
        found = []
        for i, iframe in enumerate(soup.find_all("iframe")):
            src = iframe.get("src", "")
            if is_valid_src(src) and player_pattern.search(src):
                found.append({
                    "name": "TR Addon 🇹🇷",
                    "title": f"HDFilmCehennemi #{i+1} 🔥",
                    "externalUrl": fix_src(src),
                })

        # Hiç iframe bulunamazsa tüm iframe'leri dene
        if not found:
            for i, iframe in enumerate(soup.find_all("iframe")):
                src = iframe.get("src", "")
                if is_valid_src(src):
                    found.append({
                        "name": "TR Addon 🇹🇷",
                        "title": f"HDFilmCehennemi #{i+1} 🔥",
                        "externalUrl": fix_src(src),
                    })

        if found:
            streams.extend(found)
            logger.info(f"HDFilmCehennemi: {len(found)} stream [{domain}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    await get_session()
    logger.info("=== TR Sinema Paketi baslatildi ===")


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "sources": ["Dizipal", "YabanciDizi", "HDFilmCehennemi"],
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    parts = imdb_id.split(":")
    season = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(
        f"Istek → type={video_type} id={imdb_id} s={season} e={episode}"
    )

    all_streams: List[dict] = []

    try:
        name = await get_media_name(imdb_id, video_type)

        results = await asyncio.gather(
            scrape_dizipal(name, video_type, season, episode),
            scrape_yabancidizi(name, video_type, season, episode),
            scrape_hdfilmcehennemi(name, video_type, season, episode),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Scraper hatasi: {result}")
            else:
                all_streams.extend(result)

    except Exception as e:
        logger.error(f"get_stream genel hatasi: {e}")

    logger.info(f"Toplam {len(all_streams)} stream donduruluyor.")
    return {"streams": all_streams}
