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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MANIFEST = {
    "id": "com.tr.turkce.addon.v11",
    "version": "11.0.0",
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

TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "25"))

# ScraperAPI key — render.com environment variable'dan okunur
# Dashboard → Environment → SCRAPER_API_KEY değişkenini ekle
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

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


def scraper_url(target_url: str) -> str:
    """
    Hedef URL'yi ScraperAPI proxy'si üzerinden geçirir.
    ScraperAPI kendi proxy havuzunu kullanır → Türk siteler engelleyemez.
    """
    encoded = urllib.parse.quote(target_url, safe="")
    return f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&url={encoded}"


async def fetch_html(url: str) -> Optional[str]:
    """ScraperAPI üzerinden HTML çek."""
    if not SCRAPER_API_KEY:
        logger.error("SCRAPER_API_KEY tanimlanmamis!")
        return None
    try:
        s = await get_session()
        proxy_url = scraper_url(url)
        r = await s.get(proxy_url, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
        logger.warning(f"HTTP {r.status_code} → {url}")
    except Exception as e:
        logger.error(f"fetch_html hatasi [{url}]: {e}")
    return None


async def get_media_name(imdb_id: str, video_type: str) -> str:
    """Cinemeta API'sinden film/dizi adını al (proxy gerekmez)."""
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
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    return src


def is_valid_src(src: str) -> bool:
    return bool(src and src.startswith(("http://", "https://", "//")))


# ─────────────────────────────────────────────────────────
# SCRAPER 1: DİZİPAL
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
        # Direkt URL dene
        if video_type == "series" and season and episode:
            direct_url = f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum"
        else:
            direct_url = f"{domain}/film/{slug}-izle"

        page_html = await fetch_html(direct_url)

        # Direkt olmadıysa arama yap
        if not page_html:
            search_html = await fetch_html(f"{domain}/?s={safe_name}")
            if not search_html:
                continue

            soup = BeautifulSoup(search_html, "lxml")
            link = soup.find("a", href=re.compile(r"/bolum/|/film/|/dizi/"))
            if not link:
                continue

            href = link["href"]
            if not href.startswith("http"):
                href = f"{domain}{href}"

            # Dizi için bölüm URL'si oluştur
            if video_type == "series" and season and episode:
                href = f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum"

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
                    "title": f"Dizipal #{i+1} 📺",
                    "externalUrl": fix_src(src),
                })

        if found:
            streams.extend(found)
            logger.info(f"Dizipal: {len(found)} stream [{domain}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 2: YABANCI DİZİ
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

        if video_type == "series" and season and episode:
            href = f"{href.rstrip('/')}/{season}-sezon-{episode}-bolum/"

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
    if not SCRAPER_API_KEY:
        logger.warning("SCRAPER_API_KEY tanimlanmamis! Render Environment Variables'a ekle.")
    else:
        logger.info(f"ScraperAPI aktif. Key: {SCRAPER_API_KEY[:8]}...")
    logger.info("=== TR Sinema Paketi baslatildi ===")


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scraper_api": "aktif" if SCRAPER_API_KEY else "TANIMLANMAMIS",
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    parts = imdb_id.split(":")
    season = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(f"Istek → type={video_type} id={imdb_id} s={season} e={episode}")

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
