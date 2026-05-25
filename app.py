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
    "version": "12.7.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Kredi Korumalı Ultra-Stabil Lazy-Domain Sinema Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# 2026-05 İtibarıyla Canlı/Aktif Olan Doğrulanmış Güncel Domainler
# Kod ilk başta Google'a sormadan direkt bu çalışan adresleri deneyecek!
CURRENT_ACTIVE_DOMAINS = {
    "hdfilmcehennemi": "https://www.hdfilmcehennemi.nl",
    "fullhdfilmizle": "https://www.fullhdfilmizlesene.life",
    "dizipal": "https://dizipal2073.com", 
    "yabancidizi": "https://yabancidizi.life"
}

FALLBACK_SEARCH_KEYWORDS = {
    "hdfilmcehennemi": "hdfilmcehennemi güncel adresi",
    "fullhdfilmizle": "fullhdfilmizlesene güncel adresi",
    "dizipal": "dizipal güncel adresi",
    "yabancidizi": "yabancidizi güncel adresi"
}

# Başlangıç hafızasını doğrudan çalışan canlı domainlerle dolduruyoruz (Sıfır Google Yükü)
LIVE_DOMAINS = {k: v for k, v in CURRENT_ACTIVE_DOMAINS.items()}

HTTP_TIMEOUT = 6  # Render'ın 10. saniyede shutdown etmemesi için timeout süresini daralttık

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
        logger.error("Hic scrape.do tokeni tanimlanmamis!")
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
                # Sayfa veya domain gerçekten yoksa boş dön, boşa kredi harcama
                return None
        except Exception:
            await rotate_token()
    return None

# ─────────────────────────────────────────────────────────
# AKILLI VE EKONOMİK DOMAİN YÖNETİCİSİ (KREDİ DOSTU)
# ─────────────────────────────────────────────────────────

async def resolve_via_google(site_key: str) -> Optional[str]:
    keyword = FALLBACK_SEARCH_KEYWORDS.get(site_key)
    if not keyword: return None
    
    logger.warning(f"🚨 [Kritik Durum] {site_key} adresi kapali! Google Fallback devreye giriyor...")
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
                    logger.info(f"🟢 [Google Fallback Başarılı] {site_key} Yeni Adresi Bulundu: {resolved}")
                    return resolved
    return None

async def check_and_heal_domain(site_key: str) -> str:
    """ 
    Öncelikle hafızadaki (LIVE_DOMAINS) güncel adresi doğrudan döndürür.
    Eğer o adres patlarsa (Scraper hata verirse) arka planda iyileştirme yapar.
    """
    if LIVE_DOMAINS[site_key]:
        return LIVE_DOMAINS[site_key]
        
    # Eğer bir şekilde cache temizlendiyse statik listeyi hemen canlandır
    LIVE_DOMAINS[site_key] = CURRENT_ACTIVE_DOMAINS.get(site_key, "")
    return LIVE_DOMAINS[site_key]

# ─────────────────────────────────────────────────────────
# METADATA & UTILS
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
# BAĞIMSIZ GÜVENLİ SCRAPERS
# ─────────────────────────────────────────────────────────

async def scrape_hdfilmcehennemi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    domain = await check_and_heal_domain("hdfilmcehennemi")
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
    domain = await check_and_heal_domain("fullhdfilmizle")
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
    domain = await check_and_heal_domain("dizipal")
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
    domain = await check_and_heal_domain("yabancidizi")
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
    await get_session()
    logger.info("=== Kredi Korumalı Güvenli Sürüm Başlatıldı ===")

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "tokens_active": len(SCRAPE_DO_TOKENS) > 0,
        "active_domains": LIVE_DOMAINS
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
    logger.info(f"Sorgulanan Medya: {media_name} ({video_type})")

    for source_name, scraper_func in scraper_pipeline:
        try:
            result = await scraper_func(media_name, video_type, season, episode)
            if result:
                all_streams.extend(result)
                break  # Sıralı Kısa Devre: İlk bulan kaynak akışı kilitler, gereksiz kredi harcatmaz.
        except Exception as e:
            logger.error(f"❌ {source_name} taramasında hata meydana geldi: {e}")
            # Tarama tamamen çökerse ilgili sitenin cache alanını temizle ki bir dahaki sefere Google Fallback tetiklenebilsin
            if source_name == "HDFilmCehennemi": 
                LIVE_DOMAINS["hdfilmcehennemi"] = ""
                # Eğer ilk denemede çöktüyse anında Google üzerinden taze domaini zorla
                fallback = await resolve_via_google("hdfilmcehennemi")
                if fallback: LIVE_DOMAINS["hdfilmcehennemi"] = fallback
            elif source_name == "FullHDFilmIzle":
                LIVE_DOMAINS["fullhdfilmizle"] = ""
                fallback = await resolve_via_google("fullhdfilmizle")
                if fallback: LIVE_DOMAINS["fullhdfilmizle"] = fallback
            elif source_name == "Dizipal":
                LIVE_DOMAINS["dizipal"] = ""
                fallback = await resolve_via_google("dizipal")
                if fallback: LIVE_DOMAINS["dizipal"] = fallback
            elif source_name == "YabanciDizi":
                LIVE_DOMAINS["yabancidizi"] = ""
                fallback = await resolve_via_google("yabancidizi")
                if fallback: LIVE_DOMAINS["yabancidizi"] = fallback

    return {"streams": all_streams}
