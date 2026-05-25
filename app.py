import asyncio
import logging
import os
import re
import unicodedata
import urllib.parse
from typing import Optional, List, Dict, Any

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
    "id": "com.tr.turkce.addon.v12",
    "version": "12.6.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Render Uyumlu Lazy-Domain & Sabit Kaynak Sıralı Film/Dizi Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# Ana yönlendiriciler çökerse veya yavaş kalırsa anında devreye girecek statik yedek havuzu
RESOLVER_MAP = {
    "hdfilmcehennemi": "https://www.hdfilmcehennemi.nl",
    "fullhdfilmizle": "https://www.fullhdfilmizlesene.de",
    "dizipal": "https://dizipal2073.com", 
    "yabancidizi": "https://yabancidizi.life"
}

FALLBACK_SEARCH_KEYWORDS = {
    "hdfilmcehennemi": "hdfilmcehennemi güncel adresi",
    "fullhdfilmizle": "fullhdfilmizlesene güncel adresi",
    "dizipal": "dizipal güncel adresi",
    "yabancidizi": "yabancidizi güncel adresi"
}

# Hafıza başlangıçta boş, ilk aramayla birlikte dolacak (Lazy-load cache)
LIVE_DOMAINS = {
    "hdfilmcehennemi": "",
    "fullhdfilmizle": "",
    "dizipal": "",
    "yabancidizi": ""
}

HTTP_TIMEOUT = 7  # Stremio'nun kopmaması için istek başına timeout süresini 7 saniyeye çektik

_RAW_TOKENS = [
    os.getenv("SCRAPEDO_KEY_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
    os.getenv("SCRAPEDO_KEY_2", ""),
    os.getenv("SCRAPEDO_KEY_3", ""),
    os.getenv("SCRAPEDO_KEY_4", ""),
]
SCRAPE_DO_TOKENS = [t for t in _RAW_TOKENS if t]
_token_index = 0
_token_lock: Optional[asyncio.Lock] = None

def _get_token_lock() -> asyncio.Lock:
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock

async def rotate_token():
    global _token_index
    if not SCRAPE_DO_TOKENS:
        return
    async with _get_token_lock():
        _token_index = (_token_index + 1) % len(SCRAPE_DO_TOKENS)
        logger.warning(f"🔄 scrape.do Token rotasyonu yapıldı: {_token_index + 1}. token devrede.")

async def get_active_token() -> str:
    if not SCRAPE_DO_TOKENS:
        return ""
    return SCRAPE_DO_TOKENS[_token_index]

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
    return _session

async def fetch_html(url: str) -> Optional[str]:
    if not SCRAPE_DO_TOKENS:
        return None
    
    s = await get_session()
    encoded_target = urllib.parse.quote(url, safe="")
    
    for _ in range(max(1, len(SCRAPE_DO_TOKENS))):
        token = await get_active_token()
        proxy_url = f"https://api.scrape.do?token={token}&url={encoded_target}"
        try:
            r = await s.get(proxy_url, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.text
            elif r.status_code in [401, 403, 429]:
                await rotate_token()
                continue
            elif r.status_code == 404:
                return None
        except Exception:
            await rotate_token()
    return None

# ─────────────────────────────────────────────────────────
# LAZY-DOMAİN RESOLVER MOTORU
# ─────────────────────────────────────────────────────────

async def resolve_via_google(site_key: str) -> Optional[str]:
    keyword = FALLBACK_SEARCH_KEYWORDS.get(site_key)
    if not keyword: return None
    
    logger.info(f"🔍 [Google Fallback] {site_key} aranıyor...")
    google_url = f"https://www.google.com/search?q={urllib.parse.quote(keyword)}&num=3"
    
    html = await fetch_html(google_url)
    if not html: return None
        
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/url?q=" in href:
            parsed_url = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("q")
            if parsed_url:
                actual_url = parsed_url[0]
                if "google" not in actual_url and any(x in actual_url for x in [site_key, "dizipal"]):
                    parsed_domain = urllib.parse.urlparse(actual_url)
                    resolved = f"{parsed_domain.scheme}://{parsed_domain.netloc}"
                    return resolved
    return None

async def update_live_domain(site_key: str) -> str:
    """ Sadece ihtiyaç anında çağrılır ve Render'ı asla kilitlemez. """
    base_url = RESOLVER_MAP.get(site_key)
    token = await get_active_token()
    if not token: return base_url

    encoded_target = urllib.parse.quote(base_url, safe="")
    proxy_url = f"https://api.scrape.do?token={token}&url={encoded_target}"
    
    try:
        s = await get_session()
        r = await s.get(proxy_url, timeout=5, allow_redirects=True)
        final_url = r.headers.get("X-Final-Url") or r.headers.get("sc-final-url") or r.url
        
        if not final_url or "scrape.do" in final_url:
            soup = BeautifulSoup(r.text, "lxml")
            canonical = soup.find("link", rel="canonical")
            if canonical and canonical.get("href"):
                final_url = canonical["href"]

        if final_url and "scrape.do" not in final_url and len(final_url) > 10:
            parsed = urllib.parse.urlparse(final_url)
            resolved_domain = f"{parsed.scheme}://{parsed.netloc}"
            LIVE_DOMAINS[site_key] = resolved_domain
            logger.info(f"🎯 Canlı Domain Çözüldü [{site_key}] ➔ {resolved_domain}")
            return resolved_domain
    except Exception:
        pass
    
    # Ana resolver yanıt vermezse arka planda hızlıca Google Fallback dene
    fallback_domain = await resolve_via_google(site_key)
    if fallback_domain:
        LIVE_DOMAINS[site_key] = fallback_domain
        return fallback_domain

    # İkisi de o anki koruma duvarına takılırsa havuzdaki güvenli statik linki dön
    LIVE_DOMAINS[site_key] = base_url
    return base_url

async def get_domain(site_key: str) -> str:
    if not LIVE_DOMAINS[site_key]:
        return await update_live_domain(site_key)
    return LIVE_DOMAINS[site_key]

# ─────────────────────────────────────────────────────────
# UTILS & SEARCH ENGINE
# ─────────────────────────────────────────────────────────

async def get_media_name(imdb_id: str, video_type: str) -> str:
    pure_id = imdb_id.split(":")[0]
    try:
        s = await get_session()
        url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
        r = await s.get(url, timeout=4)
        if r.status_code == 200:
            name = r.json().get("meta", {}).get("name", "")
            if name: return name
    except Exception:
        pass
    return pure_id

def to_slug(text: str) -> str:
    if not text: return ""
    tr_map = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisoucgisou")
    text = text.translate(tr_map)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text

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
        if src and (src.startswith("http://") or src.startswith("https://")):
            provider_title = detect_player_provider(src)
            found.append({
                "name": "TR Addon 🇹🇷",
                "title": f"{source_name} ➔ {provider_title} {emoji}",
                "externalUrl": src,
            })
    return found

def get_best_search_result(soup: BeautifulSoup, name: str, video_type: str, domain: str) -> Optional[str]:
    target_slug = to_slug(name)
    target_words = [w for w in target_slug.split("-") if len(w) > 1]
    if not target_words: target_words = [target_slug]
    best_link = None
    max_score = 0
    
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text_slug = to_slug(a.get_text(strip=True))
        if any(x in href for x in ["/kategori/", "/oyuncu/", "/etiket/", "/tur/", "/yapim-yili/", "?s=", "/page/"]):
            continue
        if video_type == "movie" and ("dizi" in href and "film" not in href):
            continue
        if video_type == "series" and ("film" in href and "dizi" not in href and "bolum" not in href):
            continue
            
        score = 0
        for word in target_words:
            if word in href: score += 2
            if word in text_slug: score += 2
        if target_slug in href or target_slug in text_slug: score += 15
            
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
        rf"(?:sezon|s)\s*0?{s_num}.*?(?:bolum|bölüm|e)\s*0?{e_num}\b"
    ]
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        for c in compiled:
            if c.search(href) or c.search(text):
                link = a["href"]
                if not link.startswith("http"):
                    link = f"{domain.rstrip('/')}/{link.lstrip('/')}"
                return link
    return None

# ─────────────────────────────────────────────────────────
# CORE SCRAPERS
# ─────────────────────────────────────────────────────────

async def scrape_hdfilmcehennemi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    domain = await get_domain("hdfilmcehennemi")
    search_html = await fetch_html(f"{domain}/?s={urllib.parse.quote_plus(name)}")
    if not search_html: return streams
    
    target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
    if not target_url: return streams

    if video_type == "series" and season and episode:
        main_page_html = await fetch_html(target_url)
        if not main_page_html: return streams
        ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
        if ep_url: target_url = ep_url
        else: return streams

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "HDFilmCehennemi", "🔥"))
    return streams

async def scrape_fullhdfilmizle(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    domain = await get_domain("fullhdfilmizle")
    search_html = await fetch_html(f"{domain}/search?q={urllib.parse.quote_plus(name)}")
    if not search_html: return streams
        
    target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
    if not target_url: return streams
        
    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "FullHDFilm", "⚡"))
    return streams

async def scrape_dizipal(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    domain = await get_domain("dizipal")
    search_html = await fetch_html(f"{domain}/?s={urllib.parse.quote_plus(name)}")
    if not search_html: return streams
    
    target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
    if not target_url: return streams

    if video_type == "series" and season and episode:
        if not get_episode_link(BeautifulSoup(f'<a href="{target_url}"></a>', 'lxml'), domain, season, episode):
            main_page_html = await fetch_html(target_url)
            if not main_page_html: return streams
            ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
            if ep_url: target_url = ep_url
            else: return streams

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "Dizipal", "📺"))
    return streams

async def scrape_yabancidizi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    domain = await get_domain("yabancidizi")
    search_html = await fetch_html(f"{domain}/arama?q={urllib.parse.quote_plus(name)}")
    if not search_html: return streams
        
    target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
    if not target_url: return streams
        
    if video_type == "series" and season and episode:
        main_page_html = await fetch_html(target_url)
        if not main_page_html: return streams
        ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
        if not ep_url: return streams
        target_url = ep_url
            
    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "YabanciDizi", "🚀"))
    return streams

# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    # Render'ı kitlememek için açılışta asenkron çözücüleri tetiklemiyoruz! Sunucu anında açılacak.
    await get_session()
    logger.info("=== Render Uyumlu Güvenli TR Sinema Paketi Başlatıldı ===")

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "tokens_active": len(SCRAPE_DO_TOKENS) > 0,
        "cached_domains": LIVE_DOMAINS
    }

@app.get("/manifest.json")
def get_manifest():
    return MANIFEST

@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    parts = imdb_id.split(":")
    season = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    if video_type == "movie":
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("FullHDFilmIzle", scrape_fullhdfilmizle),
            ("YabanciDizi", scrape_yabancidizi)
        ]
    else:
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("Dizipal", scrape_dizipal),
            ("YabanciDizi", scrape_yabancidizi)
        ]

    all_streams: List[dict] = []
    media_name = await get_media_name(imdb_id, video_type)

    for source_name, scraper_func in scraper_pipeline:
        try:
            result = await scraper_func(media_name, video_type, season, episode)
            if result:
                all_streams.extend(result)
                break  # Kısa devre (Maksimum 3 scrape kuralı ve Bütçe Koruma)
        except Exception as e:
            logger.error(f"❌ {source_name} hatası: {e}")
            # Domain patladıysa bir sonraki istekte taze çözülmesi için cache sıfırlanır
            if source_name == "HDFilmCehennemi": LIVE_DOMAINS["hdfilmcehennemi"] = ""
            elif source_name == "FullHDFilmIzle": LIVE_DOMAINS["fullhdfilmizle"] = ""
            elif source_name == "Dizipal": LIVE_DOMAINS["dizipal"] = ""
            elif source_name == "YabanciDizi": LIVE_DOMAINS["yabancidizi"] = ""

    return {"streams": all_streams}
