import asyncio
import gc
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from typing import Optional, List, Dict, Any, Tuple

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─────────────────────────────────────────────────────────
# LOGGING & APP SETUP
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="TR Sinema Paketi Addon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MANIFEST: Dict[str, Any] = {
    "id": "com.tr.turkce.addon.v14",
    "version": "14.1.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Paralel Tarama Motorlu Gelişmiş Sinema Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
}

# ─────────────────────────────────────────────────────────
# DOMAIN & TOKEN CONFIG
# ─────────────────────────────────────────────────────────
DOMAINS_JSON_URL = os.getenv("DOMAINS_JSON_URL", "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/domains.json")

_FALLBACK_DOMAINS: Dict[str, str] = {
    "hdfilmcehennemi": "https://www.hdfilmcehennemi.nl",
    "fullhdfilmizle":  "https://www.fullhdfilmizlesene.life",
    "dizipal":         "https://dizipal2073.com",
    "yabancidizi":     "https://yabancidizi.life",
}

CURRENT_ACTIVE_DOMAINS: Dict[str, str] = dict(_FALLBACK_DOMAINS)
_DOMAINS_CACHE_TTL   = 21_600  
_domains_last_fetch  = 0.0

STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600  
REQUEST_TIMEOUT = 8  # İstek başına maksimum 8 saniye (Stremio limiti aşılmasın diye)

_RAW_TOKENS = [
    os.getenv("SCRAPEDO_KEY_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
    os.getenv("SCRAPEDO_KEY_2", "ad94f3583fe44d0ca3635c5af37b73f2c64d90c0a59"),
    os.getenv("SCRAPEDO_KEY_3", ""),
    os.getenv("SCRAPEDO_KEY_4", ""),
]
SCRAPE_DO_TOKENS: List[str] = [t for t in _RAW_TOKENS if t]
_token_index = 0

def get_active_token() -> str:
    global _token_index
    return SCRAPE_DO_TOKENS[_token_index] if SCRAPE_DO_TOKENS else ""

def rotate_token() -> None:
    global _token_index
    if SCRAPE_DO_TOKENS:
        _token_index = (_token_index + 1) % len(SCRAPE_DO_TOKENS)

async def refresh_domains_if_needed() -> None:
    global CURRENT_ACTIVE_DOMAINS, _domains_last_fetch
    if time.time() - _domains_last_fetch < _DOMAINS_CACHE_TTL:
        return
    try:
        async with AsyncSession() as s:
            r = await s.get(DOMAINS_JSON_URL, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
                    CURRENT_ACTIVE_DOMAINS = data
                    _domains_last_fetch = time.time()
                    return
    except Exception:
        pass
    if not _domains_last_fetch:
        CURRENT_ACTIVE_DOMAINS = dict(_FALLBACK_DOMAINS)
    _domains_last_fetch = time.time()

# ─────────────────────────────────────────────────────────
# HTTP & UTILS
# ─────────────────────────────────────────────────────────
async def fetch_html(url: str) -> Optional[str]:
    token = get_active_token()
    if not token: return None
    proxy_url = f"https://api.scrape.do?token={token}&url={urllib.parse.quote(url, safe='')}"
    try:
        async with AsyncSession(impersonate="chrome120") as s:
            r = await s.get(proxy_url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200: return r.text
            if r.status_code in (401, 403, 429): rotate_token()
            return None
    except Exception:
        return None

async def get_media_name(imdb_id: str, video_type: str) -> str:
    pure_id = imdb_id.split(":")[0]
    url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
    try:
        async with AsyncSession() as s:
            r = await s.get(url, timeout=5)
            if r.status_code == 200:
                name = r.json().get("meta", {}).get("name", "")
                if name: return re.sub(r"\s*\((movie|series|show)\)\s*$", "", name, flags=re.IGNORECASE).strip()
    except Exception:
        pass
    return pure_id

def to_slug(text: str) -> str:
    if not text: return ""
    tr_map = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisoucgisou")
    text = text.translate(tr_map).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9\s-]", "", text).strip().replace(" ", "-"))

def detect_player_provider(url: str) -> str:
    u = url.lower()
    if "vidmoly" in u: return "Vidmoly 🟣"
    if "fembed" in u or "feurl" in u: return "Fembed 🟠"
    if "ok.ru" in u or "odnoklassniki" in u: return "OK.ru 🟤"
    if "vidoza" in u: return "Vidoza 🟢"
    if "streamtape" in u: return "Streamtape 🔵"
    if "voe.sx" in u: return "VOE 🟡"
    if "dood" in u: return "DoodStream 🔴"
    if "plusplayer" in u or "moly" in u: return "Hızlı Player ⚡"
    return "Yayın Sunucusu 🌐"

def extract_iframes(soup: BeautifulSoup, source_name: str, emoji: str) -> List[Dict[str, Any]]:
    found = []
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "").strip()
        if src.startswith("//"): src = "https:" + src
        if src.startswith("http"):
            found.append({
                "name": "TR Addon 🇹🇷",
                "title": f"{source_name} ➔ {detect_player_provider(src)} {emoji}",
                "externalUrl": src,
            })
    return found

def get_best_search_result(soup: BeautifulSoup, name: str, video_type: str, domain: str) -> Optional[str]:
    target_slug = to_slug(name)
    target_words = [w for w in target_slug.split("-") if len(w) > 1] or [target_slug]
    best_link, max_score = None, 0

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text_slug = to_slug(a.get_text(strip=True))
        if any(x in href for x in ["/kategori/", "/oyuncu/", "/etiket/", "/tur/", "/yapim-yili/", "?s=", "/page/"]): continue
        if video_type == "movie" and ("dizi" in href and "film" not in href): continue
        if video_type == "series" and ("film" in href and "dizi" not in href and "bolum" not in href): continue

        score = sum(2 for w in target_words if w in href or w in text_slug)
        if target_slug in href or target_slug in text_slug: score += 15
        if score > max_score and score > 0:
            max_score = score
            best_link = a["href"]

    if best_link and not best_link.startswith("http"):
        best_link = f"{domain.rstrip('/')}/{best_link.lstrip('/')}"
    return best_link

def get_episode_link(soup: BeautifulSoup, domain: str, season: str, episode: str) -> Optional[str]:
    s_num, e_num = int(season), int(episode)
    compiled = [re.compile(p, re.IGNORECASE) for p in [rf"s0?{s_num}\s?e0?{e_num}\b", rf"\b0?{s_num}x0?{e_num}\b"]]
    for a in soup.find_all("a", href=True):
        if any(c.search(a["href"]) or c.search(a.get_text()) for c in compiled):
            link = a["href"]
            return link if link.startswith("http") else f"{domain.rstrip('/')}/{link.lstrip('/')}"
    return None

# ─────────────────────────────────────────────────────────
# PARALEL PARSER MOTORU
# ─────────────────────────────────────────────────────────
async def _base_scraper(key: str, source_name: str, emoji: str, name: str, video_type: str, season: Optional[str], episode: Optional[str], url_formatter) -> List[Dict[str, Any]]:
    domain = CURRENT_ACTIVE_DOMAINS.get(key, _FALLBACK_DOMAINS[key])
    try:
        search_html = await fetch_html(url_formatter(domain, name))
        if not search_html: return []
        
        target_url = get_best_search_result(BeautifulSoup(search_html, "html.parser"), name, video_type, domain)
        if not target_url: return []

        if video_type == "series" and season and episode:
            main_html = await fetch_html(target_url)
            if not main_html: return []
            target_url = get_episode_link(BeautifulSoup(main_html, "html.parser"), domain, season, episode) or target_url

        page_html = await fetch_html(target_url)
        if page_html:
            return extract_iframes(BeautifulSoup(page_html, "html.parser"), source_name, emoji)
    except Exception:
        pass
    return []

async def scrape_hdfilmcehennemi(n, t, s, e): return await _base_scraper("hdfilmcehennemi", "HDFilmCehennemi", "🔥", n, t, s, e, lambda d, name: f"{d}/?s={urllib.parse.quote_plus(name)}")
async def scrape_fullhdfilmizle(n, t, s, e):  return await _base_scraper("fullhdfilmizle", "FullHDFilm", "⚡", n, t, s, e, lambda d, name: f"{d}/search?q={urllib.parse.quote_plus(name)}")
async def scrape_dizipal(n, t, s, e):         return await _base_scraper("dizipal", "Dizipal", "📺", n, t, s, e, lambda d, name: f"{d}/?s={urllib.parse.quote_plus(name)}")
async def scrape_yabancidizi(n, t, s, e):     return await _base_scraper("yabancidizi", "YabanciDizi", "🚀", n, t, s, e, lambda d, name: f"{d}/arama?q={urllib.parse.quote_plus(name)}")

# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────
@app.get("/")
def health(): return {"status": "ok", "engine": "parallel_async"}

@app.get("/manifest.json")
def get_manifest(): return MANIFEST

@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str) -> Dict[str, Any]:
    await refresh_domains_if_needed()
    cache_key = f"{video_type}_{imdb_id}"
    
    if cache_key in STREAM_CACHE and (time.time() - STREAM_CACHE[cache_key]["timestamp"] < CACHE_TTL):
        return {"streams": STREAM_CACHE[cache_key]["streams"]}

    parts = imdb_id.split(":")
    season  = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    media_name = await get_media_name(imdb_id, video_type)
    logger.info(f"🎬 Paralel Sorgu Başlatıldı: {media_name} ({video_type})")

    # Pipeline eşleşmesi
    if video_type == "movie":
        tasks = [
            scrape_hdfilmcehennemi(media_name, video_type, season, episode),
            scrape_fullhdfilmizle(media_name, video_type, season, episode),
            scrape_yabancidizi(media_name, video_type, season, episode)
        ]
    else:
        tasks = [
            scrape_hdfilmcehennemi(media_name, video_type, season, episode),
            scrape_dizipal(media_name, video_type, season, episode),
            scrape_yabancidizi(media_name, video_type, season, episode)
        ]

    # 🔥 BÜYÜLÜ DOKUNUŞ: Tüm siteleri aynı anda tarıyoruz
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_streams = []
    for res in results:
        if res and isinstance(res, list):
            all_streams.extend(res)

    # Öncelik sıralaması (Hızlı player'lar en üste)
    _priority = {"Hızlı Player ⚡": 1, "Vidmoly 🟣": 2, "VOE 🟡": 3, "Vidoza 🟢": 4, "Streamtape 🔵": 5}
    all_streams.sort(key=lambda x: next((v for k, v in _priority.items() if k in x["title"]), 50))

    STREAM_CACHE[cache_key] = {"timestamp": time.time(), "streams": all_streams}
    gc.collect()

    return {"streams": all_streams}
