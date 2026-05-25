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
    "version": "11.2.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Türkçe Dublaj & Altyazılı Film/Dizi — Dizipal, YabanciDizi, HDFilmCehennemi, FullHDFilmIzle",
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

FULLHDFILM_DOMAINS = [
    "https://www.fullhdfilmizlesene.de",
    "https://www.fullhdfilmizlesene.com",
    "https://www.fullhdfilmizlesene.pw",
]

TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "25"))

# ScraperAPI key rotasyonu
_RAW_KEYS = [
    os.getenv("SCRAPER_API_KEY_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
    os.getenv("SCRAPER_API_KEY_2", ""),
    os.getenv("SCRAPER_API_KEY_3", ""),
    os.getenv("SCRAPER_API_KEY_4", ""),
]
SCRAPER_API_KEYS = [k for k in _RAW_KEYS if k]
_key_index = 0
_key_lock: Optional[asyncio.Lock] = None


def _get_key_lock() -> asyncio.Lock:
    global _key_lock
    if _key_lock is None:
        _key_lock = asyncio.Lock()
    return _key_lock


async def rotate_key():
    global _key_index
    async with _get_key_lock():
        _key_index = (_key_index + 1) % len(SCRAPER_API_KEYS)
        logger.warning(f"Key rotasyonu → {_key_index + 1}. key'e geçildi.")


async def get_active_key() -> str:
    if not SCRAPER_API_KEYS:
        return ""
    return SCRAPER_API_KEYS[_key_index]


SCRAPER_API_KEY = SCRAPER_API_KEYS[0] if SCRAPER_API_KEYS else ""

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
            logger.info("AsyncSession oluşturuldu.")
    return _session


async def fetch_html(url: str) -> Optional[str]:
    """ScraperAPI üzerinden HTML çek. 429 gelince otomatik key rotasyonu yapar."""
    if not SCRAPER_API_KEYS:
        logger.error("Hiç SCRAPER_API_KEY tanımlanmamış!")
        return None
    try:
        s = await get_session()
        key = await get_active_key()
        encoded = urllib.parse.quote(url, safe="")
        proxy_url = f"https://api.scraperapi.com?api_key={key}&url={encoded}"
        r = await s.get(proxy_url, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
        elif r.status_code == 429:
            logger.warning("429 limit doldu, key rotasyonu yapılıyor...")
            await rotate_key()
            key = await get_active_key()
            proxy_url = f"https://api.scraperapi.com?api_key={key}&url={encoded}"
            r2 = await s.get(proxy_url, timeout=TIMEOUT)
            if r2.status_code == 200:
                return r2.text
        logger.warning(f"HTTP {r.status_code} → {url}")
    except Exception as e:
        logger.error(f"fetch_html hatası [{url}]: {e}")
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
                logger.info(f"Cinemeta adı: {name}")
                return name
    except Exception as e:
        logger.warning(f"Cinemeta hatası: {e}")
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


def normalize_domain(domain: str) -> str:
    """Domain sonundaki slash'ı temizle."""
    return domain.rstrip("/")


# ─────────────────────────────────────────────────────────
# AKILLI EPISODE BULUCU — URL pattern tahmininden bağımsız
# ─────────────────────────────────────────────────────────
def find_episode_link(soup: BeautifulSoup, domain: str, season: str, episode: str) -> Optional[str]:
    """
    Bir dizi ana sayfasındaki tüm <a> linklerini tarayarak
    doğru season/episode linkini bulur.
    URL pattern'ini tahmin etmek yerine sayfadaki gerçek linkleri kullanır.

    Desteklenen URL örüntüleri (hepsi otomatik algılanır):
      /dizi/xxx/sezon-1/bolum-1/
      /dizi/xxx/sezon-1/bolum-1-hd14/
      /dizi/xxx-izle-157/sezon-1/bolum-1/
      /dizi/xxx/s01e01/
      /season-1/episode-1/
    """
    s_num = int(season)
    e_num = int(episode)
    dom = normalize_domain(domain)

    all_links = soup.find_all("a", href=True)
    candidates = []

    for a in all_links:
        href = a.get("href", "")
        if not href:
            continue

        # Pattern 1: sezon-N + bolum-M (Türkçe siteler — N veya 0N)
        if re.search(rf"sezon-0?{s_num}\b", href, re.I) and \
           re.search(rf"bolum-0?{e_num}(?:\b|-|/|$)", href, re.I):
            candidates.append(href)
            continue

        # Pattern 2: s01e01 tarzı
        if re.search(rf"s{s_num:02d}e{e_num:02d}", href, re.I):
            candidates.append(href)
            continue

        # Pattern 3: season-N + episode-M (İngilizce)
        if re.search(rf"season-0?{s_num}\b", href, re.I) and \
           re.search(rf"episode-0?{e_num}(?:\b|-|/|$)", href, re.I):
            candidates.append(href)
            continue

        # Pattern 4: /N-sezon/ + /M-bolum/
        if re.search(rf"/{s_num}-sezon", href, re.I) and \
           re.search(rf"/{e_num}-bolum", href, re.I):
            candidates.append(href)
            continue

    if not candidates:
        return None

    # En spesifik (en uzun) linki tercih et
    best = max(candidates, key=len)

    if best.startswith("http://") or best.startswith("https://"):
        return best
    return dom + "/" + best.lstrip("/")


def find_content_link(soup: BeautifulSoup, domain: str, video_type: str) -> Optional[str]:
    """
    Arama sonucu sayfasından içerik linkini bulur.
    video_type'a göre film veya dizi linkini döndürür.
    """
    dom = normalize_domain(domain)
    # Önce type'a uygun pattern dene
    if video_type == "series":
        pattern = re.compile(r"dizi|series|serial|show", re.I)
    else:
        pattern = re.compile(r"film|movie|izle", re.I)

    link = soup.find("a", href=pattern)
    if not link:
        # Genel link bul (herhangi bir içerik linki)
        link = soup.find("a", href=re.compile(r"dizi|film|izle|series|movie", re.I))

    if not link:
        return None

    href = link["href"]
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return dom + "/" + href.lstrip("/")


async def get_iframes_from_page(page_html: str, label: str, emoji: str) -> List[dict]:
    """Sayfadaki iframe src'lerini döndürür."""
    soup = BeautifulSoup(page_html, "lxml")
    found = []
    for i, iframe in enumerate(soup.find_all("iframe")):
        src = iframe.get("src", "")
        if is_valid_src(src):
            found.append({
                "name": "TR Addon 🇹🇷",
                "title": f"{label} #{i+1} {emoji}",
                "externalUrl": fix_src(src),
            })
    return found


# ─────────────────────────────────────────────────────────
# SCRAPER 1 — DİZİPAL  (sadece dizi)
# ─────────────────────────────────────────────────────────
async def scrape_dizipal(
    name: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    streams = []
    if not name or not season or not episode:
        return streams

    slug = to_slug(name)
    safe_name = urllib.parse.quote_plus(name)

    for domain in DIZIPAL_DOMAINS:
        dom = normalize_domain(domain)
        found = []

        # 1) Direkt URL dene
        direct_url = f"{dom}/bolum/{slug}-{season}-sezon-{episode}-bolum"
        page_html = await fetch_html(direct_url)

        if page_html:
            found = await get_iframes_from_page(page_html, "Dizipal", "📺")

        # 2) Direkt olmadıysa → arama yap → show sayfası → episode link tara
        if not found:
            search_html = await fetch_html(f"{dom}/?s={safe_name}")
            if not search_html:
                continue

            soup = BeautifulSoup(search_html, "lxml")
            show_href = find_content_link(soup, dom, "series")
            if not show_href:
                continue

            show_html = await fetch_html(show_href)
            if not show_html:
                continue

            show_soup = BeautifulSoup(show_html, "lxml")

            # Akıllı episode link arayıcı
            ep_url = find_episode_link(show_soup, dom, season, episode)

            if ep_url:
                page_html = await fetch_html(ep_url)
                if page_html:
                    found = await get_iframes_from_page(page_html, "Dizipal", "📺")
            else:
                # Son çare: show sayfasının kendi iframe'leri
                found = await get_iframes_from_page(show_html, "Dizipal", "📺")

        if found:
            streams.extend(found)
            logger.info(f"Dizipal {len(found)} stream [{dom}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 2 — YABANCI DİZİ  (sadece dizi)
# ─────────────────────────────────────────────────────────
async def scrape_yabancidizi(
    name: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    streams = []
    if not name or not season or not episode:
        return streams

    safe_name = urllib.parse.quote_plus(name)

    for domain in YABANCIDIZI_DOMAINS:
        dom = normalize_domain(domain)
        found = []

        search_html = await fetch_html(f"{dom}/arama?q={safe_name}")
        if not search_html:
            continue

        soup = BeautifulSoup(search_html, "lxml")
        show_href = find_content_link(soup, dom, "series")
        if not show_href:
            continue

        show_html = await fetch_html(show_href)
        if not show_html:
            continue

        show_soup = BeautifulSoup(show_html, "lxml")

        # Akıllı episode link arayıcı
        ep_url = find_episode_link(show_soup, dom, season, episode)

        if ep_url:
            page_html = await fetch_html(ep_url)
            if page_html:
                found = await get_iframes_from_page(page_html, "YabanciDizi", "🚀")
        else:
            found = await get_iframes_from_page(show_html, "YabanciDizi", "🚀")

        if found:
            streams.extend(found)
            logger.info(f"YabanciDizi {len(found)} stream [{dom}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 3 — HD FİLM CEHENNEMİ  (film + dizi)
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
        dom = normalize_domain(domain)
        found = []

        search_html = await fetch_html(f"{dom}/?s={safe_name}")
        if not search_html:
            continue

        soup = BeautifulSoup(search_html, "lxml")
        content_href = find_content_link(soup, dom, video_type)
        if not content_href:
            continue

        if video_type == "movie":
            # Film → direkt içerik sayfasından iframe al
            page_html = await fetch_html(content_href)
            if page_html:
                found = await get_iframes_from_page(page_html, "HDFilmCehennemi", "🔥")

        else:
            # Dizi → show sayfasına git, episode linkini tara
            show_html = await fetch_html(content_href)
            if not show_html:
                continue

            show_soup = BeautifulSoup(show_html, "lxml")
            ep_url = find_episode_link(show_soup, dom, season, episode)

            if ep_url:
                page_html = await fetch_html(ep_url)
                if page_html:
                    found = await get_iframes_from_page(page_html, "HDFilmCehennemi", "🔥")
            else:
                # Show sayfasının iframe'lerini dene
                found = await get_iframes_from_page(show_html, "HDFilmCehennemi", "🔥")

        if found:
            streams.extend(found)
            logger.info(f"HDFilmCehennemi {len(found)} stream [{dom}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# SCRAPER 4 — FULLHDFILM İZLE  (sadece film)
# ─────────────────────────────────────────────────────────
async def scrape_fullhdfilmizle(
    name: str,
) -> List[dict]:
    streams = []
    if not name:
        return streams

    safe_name = urllib.parse.quote_plus(name)

    for domain in FULLHDFILM_DOMAINS:
        dom = normalize_domain(domain)
        found = []

        search_html = await fetch_html(f"{dom}/search?q={safe_name}")
        if not search_html:
            continue

        soup = BeautifulSoup(search_html, "lxml")
        content_href = find_content_link(soup, dom, "movie")
        if not content_href:
            continue

        page_html = await fetch_html(content_href)
        if page_html:
            found = await get_iframes_from_page(page_html, "FullHDFilm", "⚡")

        if found:
            streams.extend(found)
            logger.info(f"FullHDFilmIzle {len(found)} stream [{dom}]")
            break

    return streams


# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    await get_session()
    if not SCRAPER_API_KEYS:
        logger.warning("Hiç SCRAPER_API_KEY tanımlanmamış!")
    else:
        logger.info(f"ScraperAPI aktif. {len(SCRAPER_API_KEYS)} key yüklendi.")
    logger.info("=== TR Sinema Paketi başlatıldı ===")


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scraper_api": f"{len(SCRAPER_API_KEYS)} key aktif" if SCRAPER_API_KEYS else "TANIMLANMAMIŞ",
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    parts = imdb_id.split(":")
    season = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(f"İstek → type={video_type} id={imdb_id} s={season} e={episode}")

    all_streams: List[dict] = []

    try:
        name = await get_media_name(imdb_id, video_type)

        # ── TYPE-BASED SCRAPER SEÇİMİ ─────────────────────────────
        # Film ise: sadece film scraperları çalışır (dizi scraperlarını atla)
        # Dizi ise: sadece dizi scraperları + HDFilmCehennemi çalışır
        # ──────────────────────────────────────────────────────────
        if video_type == "movie":
            logger.info("Film isteği → Dizipal & YabanciDizi atlandı.")
            tasks = [
                scrape_hdfilmcehennemi(name, "movie", None, None),
                scrape_fullhdfilmizle(name),
            ]
        else:  # series
            logger.info("Dizi isteği → FullHDFilmIzle atlandı.")
            tasks = [
                scrape_dizipal(name, season, episode),
                scrape_yabancidizi(name, season, episode),
                scrape_hdfilmcehennemi(name, "series", season, episode),
            ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Scraper hatası: {result}")
            else:
                all_streams.extend(result)

    except Exception as e:
        logger.error(f"get_stream genel hatası: {e}")

    logger.info(f"Toplam {len(all_streams)} stream döndürülüyor.")
    return {"streams": all_streams}
