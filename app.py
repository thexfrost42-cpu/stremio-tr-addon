import asyncio
import gc
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from contextlib import asynccontextmanager
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
    "version": "14.0.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Önbellek Korumalı (Anti-Spam) & Dinamik Kaynaklı Sinema Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ─────────────────────────────────────────────────────────
# FAZ 4: DİNAMİK DOMAIN YÖNETİMİ & ASYNC RETRY
# ─────────────────────────────────────────────────────────
DOMAINS_JSON_URL = os.getenv(
    "DOMAINS_JSON_URL",
    "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/domains.json"
)

_FALLBACK_DOMAINS: Dict[str, str] = {
    "hdfilmcehennemi": "https://www.hdfilmcehennemi.nl",
    "fullhdfilmizle":  "https://www.fullhdfilmizlesene.life",
    "dizipal":         "https://dizipal2073.com",
    "yabancidizi":     "https://yabancidizi.life",
}

CURRENT_ACTIVE_DOMAINS: Dict[str, str] = dict(_FALLBACK_DOMAINS)
_DOMAINS_CACHE_TTL   = 21_600  # 6 saat
_domains_last_fetch  = 0.0

async def refresh_domains_if_needed() -> None:
    global CURRENT_ACTIVE_DOMAINS, _domains_last_fetch
    if time.time() - _domains_last_fetch < _DOMAINS_CACHE_TTL:
        return
    
    try:
        async with AsyncSession() as s:
            r = await s.get(DOMAINS_JSON_URL, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
                    CURRENT_ACTIVE_DOMAINS = data
                    _domains_last_fetch = time.time()
                    logger.info(f"✅ Domain listesi GitHub'dan güncellendi.")
                    return
    except Exception as e:
        logger.warning(f"⚠️ Dinamik domain çekilemedi (Yedek devrede): {e}")
    
    if not _domains_last_fetch:
        CURRENT_ACTIVE_DOMAINS = dict(_FALLBACK_DOMAINS)
    _domains_last_fetch = time.time()

# ─────────────────────────────────────────────────────────
# DOMAIN İZLEME & ANALİZ
# ─────────────────────────────────────────────────────────
DOMAIN_STATS: Dict[str, Dict[str, int]] = {
    key: {"success": 0, "fail": 0} for key in _FALLBACK_DOMAINS
}

def record_domain_status(key: str, success: bool) -> None:
    if key in DOMAIN_STATS:
        if success:
            DOMAIN_STATS[key]["success"] += 1
        else:
            DOMAIN_STATS[key]["fail"] += 1

def get_domain_health() -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for key, stats in DOMAIN_STATS.items():
        total = stats["success"] + stats["fail"]
        rate  = round(stats["success"] / total * 100, 1) if total > 0 else None
        report[key] = {**stats, "success_rate": f"{rate}%" if rate is not None else "N/A"}
    return report

# ─────────────────────────────────────────────────────────
# CACHE & TOKEN YÖNETİMİ
# ─────────────────────────────────────────────────────────
STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600  
REQUEST_TIMEOUT = 9  # Loglardaki curl(28) hatasını önlemek için 9 saniyeye esnetildi (Stremio limiti ~10s)

_RAW_TOKENS = [
    os.getenv("SCRAPEDO_KEY_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
    os.getenv("SCRAPEDO_KEY_2", ""),
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
        logger.warning(f"🔄 Token rotasyonu: {_token_index + 1}. token devrede.")

# ─────────────────────────────────────────────────────────
# SAF ASENKRON HTTP KATMANI (curl_cffi AsyncSession)
# ─────────────────────────────────────────────────────────
async def fetch_html(url: str) -> Optional[str]:
    token = get_active_token()
    if not token:
        logger.error("🛑 Aktif scrape.do tokeni bulunamadı!")
        return None

    encoded_target = urllib.parse.quote(url, safe="")
    proxy_url = f"https://api.scrape.do?token={token}&url={encoded_target}"

    try:
        # Arka planda OS thread'i açmadan saf asenkron istek atıyoruz (Sıfır RAM Şişmesi)
        async with AsyncSession(impersonate="chrome120") as s:
            r = await s.get(proxy_url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403, 429):
                rotate_token()
            logger.warning(f"⚠️ HTTP {r.status_code} — {url}")
            return None
    except Exception as e:
        logger.error(f"⚠️ Proxy bağlantı hatası [{url}]: {e}")
        return None

# ─────────────────────────────────────────────────────────
# ASYNC METADATA (Cinemeta)
# ─────────────────────────────────────────────────────────
_CINEMETA_SUFFIX_RE = re.compile(r"\s*\((movie|series|show)\)\s*$", re.IGNORECASE)

async def get_media_name(imdb_id: str, video_type: str) -> str:
    pure_id = imdb_id.split(":")[0]
    url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
    try:
        async with AsyncSession() as s:
            r = await s.get(url, timeout=6)
            if r.status_code == 200:
                name: str = r.json().get("meta", {}).get("name", "")
                if name:
                    return _CINEMETA_SUFFIX_RE.sub("", name).strip()
    except Exception as e:
        logger.error(f"⚠️ Cinemeta hatası [{pure_id}]: {e}")
    return pure_id

# ─────────────────────────────────────────────────────────
# PLAYER RANKING & UTILS
# ─────────────────────────────────────────────────────────
_PLAYER_PRIORITY: Dict[str, int] = {
    "Hızlı Player ⚡": 1,
    "Vidmoly 🟣":     2,
    "VOE 🟡":         3,
    "Vidoza 🟢":       4,
    "Streamtape 🔵":   5,
    "Fembed 🟠":       6,
    "OK.ru 🟤":        7,
    "DoodStream 🔴":   8,
    "Yayın Sunucusu 🌐": 99,
}

def rank_streams(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def _score(stream: Dict[str, Any]) -> int:
        title: str = stream.get("title", "")
        for player_name, priority in _PLAYER_PRIORITY.items():
            if player_name in title:
                return priority
        return 50 
    return sorted(streams, key=_score)

def to_slug(text: str) -> str:
    if not text: return ""
    tr_map = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisoucgisou")
    text = text.translate(tr_map)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", text.strip()))

def detect_player_provider(url: str) -> str:
    u = url.lower()
    if "vidmoly"      in u: return "Vidmoly 🟣"
    if "fembed"       in u or "feurl" in u: return "Fembed 🟠"
    if "ok.ru"        in u or "odnoklassniki" in u: return "OK.ru 🟤"
    if "vidoza"       in u: return "Vidoza 🟢"
    if "streamtape"   in u: return "Streamtape 🔵"
    if "voe.sx"       in u: return "VOE 🟡"
    if "dood"         in u: return "DoodStream 🔴"
    if "plusplayer"   in u or "moly" in u: return "Hızlı Player ⚡"
    return "Yayın Sunucusu 🌐"

def extract_iframes(soup: BeautifulSoup, source_name: str, emoji: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for iframe in soup.find_all("iframe"):
        src: str = iframe.get("src", "").strip()
        if src.startswith("//"): src = "https:" + src
        if src and (src.startswith("http://") or src.startswith("https://")):
            provider_title = detect_player_provider(src)
            found.append({
                "name":        "TR Addon 🇹🇷",
                "title":       f"{source_name} ➔ {provider_title} {emoji}",
                "externalUrl": src,
            })
    return found

def get_best_search_result(soup: BeautifulSoup, name: str, video_type: str, domain: str) -> Optional[str]:
    target_slug  = to_slug(name)
    target_words = [w for w in target_slug.split("-") if len(w) > 1] or [target_slug]
    best_link: Optional[str] = None
    max_score = 0

    for a in soup.find_all("a", href=True):
        href      = a["href"].lower()
        text_slug = to_slug(a.get_text(strip=True))

        if any(x in href for x in ["/kategori/", "/oyuncu/", "/etiket/", "/tur/", "/yapim-yili/", "?s=", "/page/"]):
            continue
        if video_type == "movie"  and ("dizi" in href and "film" not in href):
            continue
        if video_type == "series" and ("film" in href and "dizi" not in href and "bolum" not in href):
            continue

        score = sum(2 for w in target_words if w in href or w in text_slug)
        if target_slug in href or target_slug in text_slug:
            score += 15

        if score > max_score and score > 0:
            max_score = score
            best_link = a["href"]

    if best_link and not best_link.startswith("http"):
        best_link = f"{domain.rstrip('/')}/{best_link.lstrip('/')}"
    return best_link

def get_episode_link(soup: BeautifulSoup, domain: str, season: str, episode: str) -> Optional[str]:
    s_num, e_num = int(season), int(episode)
    patterns = [
        rf"s0?{s_num}\s?e0?{e_num}\b",
        rf"\b0?{s_num}x0?{e_num}\b",
        rf"0?{s_num}\s?\.?\s*(?:sezon|s).*?0?{e_num}\s?\.?\s*(?:bolum|bölüm|e)\b",
    ]
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        if any(c.search(href) or c.search(text) for c in compiled):
            link = a["href"]
            return link if link.startswith("http") else f"{domain.rstrip('/')}/{link.lstrip('/')}"
    return None

# ─────────────────────────────────────────────────────────
# CORE SCRAPER MİMARİSİ
# ─────────────────────────────────────────────────────────
async def _base_scraper(
    key: str, source_name: str, emoji: str, 
    name: str, video_type: str, season: Optional[str], episode: Optional[str],
    search_url_formatter
) -> List[Dict[str, Any]]:
    domain = CURRENT_ACTIVE_DOMAINS.get(key, _FALLBACK_DOMAINS[key])
    streams: List[Dict[str, Any]] = []

    search_html = await fetch_html(search_url_formatter(domain, name))
    if not search_html:
        record_domain_status(key, False); return streams

    # RAM dostu dahili html.parser kullanarak sayfayı işliyoruz
    target_url = get_best_search_result(BeautifulSoup(search_html, "html.parser"), name, video_type, domain)
    if not target_url:
        record_domain_status(key, False); return streams

    if video_type == "series" and season and episode:
        main_html = await fetch_html(target_url)
        if not main_html:
            record_domain_status(key, False); return streams
        ep_url = get_episode_link(BeautifulSoup(main_html, "html.parser"), domain, season, episode)
        if not ep_url:
            record_domain_status(key, False); return streams
        target_url = ep_url

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "html.parser"), source_name, emoji))
        record_domain_status(key, True)
    else:
        record_domain_status(key, False)
    
    return streams

async def scrape_hdfilmcehennemi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    return await _base_scraper("hdfilmcehennemi", "HDFilmCehennemi", "🔥", name, video_type, season, episode,
                               lambda d, n: f"{d}/?s={urllib.parse.quote_plus(n)}")

async def scrape_fullhdfilmizle(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    return await _base_scraper("fullhdfilmizle", "FullHDFilm", "⚡", name, video_type, season, episode,
                               lambda d, n: f"{d}/search?q={urllib.parse.quote_plus(n)}")

async def scrape_dizipal(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    return await _base_scraper("dizipal", "Dizipal", "📺", name, video_type, season, episode,
                               lambda d, n: f"{d}/?s={urllib.parse.quote_plus(n)}")

async def scrape_yabancidizi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    return await _base_scraper("yabancidizi", "YabanciDizi", "🚀", name, video_type, season, episode,
                               lambda d, n: f"{d}/arama?q={urllib.parse.quote_plus(n)}")

# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────
@app.get("/")
async def health_check() -> Dict[str, Any]:
    return {
        "status":        "ok",
        "addon":         MANIFEST["name"],
        "version":       MANIFEST["version"],
        "cache_size":    len(STREAM_CACHE),
        "tokens_active": len(SCRAPE_DO_TOKENS) > 0,
        "active_domains": CURRENT_ACTIVE_DOMAINS,
        "domain_health": get_domain_health(),
    }

@app.get("/manifest.json")
def get_manifest() -> Dict[str, Any]:
    return MANIFEST

@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str) -> Dict[str, Any]:
    await refresh_domains_if_needed()

    cache_key    = f"{video_type}_{imdb_id}"
    current_time = time.time()

    if cache_key in STREAM_CACHE:
        cached = STREAM_CACHE[cache_key]
        if current_time - cached["timestamp"] < CACHE_TTL:
            logger.info(f"⚡ Önbellekten yüklendi: {imdb_id}")
            return {"streams": cached["streams"]}
        del STREAM_CACHE[cache_key]

    parts   = imdb_id.split(":")
    season  = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    if video_type == "movie":
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("FullHDFilmIzle",  scrape_fullhdfilmizle),
            ("YabanciDizi",     scrape_yabancidizi),
        ]
    else:
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("Dizipal",         scrape_dizipal),
            ("YabanciDizi",     scrape_yabancidizi),
        ]

    all_streams: List[Dict[str, Any]] = []
    media_name = await get_media_name(imdb_id, video_type)
    logger.info(f"🎬 Sorgu: {media_name} ({video_type}) | S:{season} E:{episode}")

    for source_name, scraper_func in scraper_pipeline:
        try:
            result = await scraper_func(media_name, video_type, season, episode)
            if result:
                logger.info(f"✅ Yayın bulundu → {source_name} (Short-Circuit)")
                all_streams.extend(rank_streams(result))
                break 
        except Exception as e:
            logger.error(f"❌ {source_name} kritik tarama hatası: {e}")

    STREAM_CACHE[cache_key] = {
        "timestamp": current_time,
        "streams":   all_streams,
    }

    # Çöp toplayıcıyı zorla tetikleyerek RAM'i tamamen tahliye et
    gc.collect()

    return {"streams": all_streams}
