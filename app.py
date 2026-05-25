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
    "version": "12.0.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Akıllı URL & Sabit Kaynak Sıralı Film/Dizi Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

DIZIPAL_DOMAINS = ["https://dizipal2073.com", "https://dizipal2066.com", "https://dizipal1548.com"]
YABANCIDIZI_DOMAINS = ["https://yabancidizi.org", "https://yabancidizi.tv", "https://yabancidizi.net"]
HDFILM_DOMAINS = ["https://www.hdfilmcehennemi.life", "https://www.hdfilmcehennemi.nl", "https://www.hdfilmcehennemi.net"]
FULLHDFILM_DOMAINS = ["https://www.fullhdfilmizlesene.de", "https://www.fullhdfilmizlesene.com", "https://www.fullhdfilmizlesene.pw"]

# Tekil isteklerin kilitlenmemesi için 8 saniye idealdir
HTTP_TIMEOUT = 8

# scrape.do 4'lü Token Yönetimi
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
        logger.warning(f"scrape.do Token değiştirildi: {_token_index + 1}. token devrede.")

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
            logger.info("AsyncSession oluşturuldu.")
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
                logger.warning(f"scrape.do hatası ({r.status_code}). Token rotasyonu yapılıyor...")
                await rotate_token()
                continue
            elif r.status_code == 404:
                logger.warning(f"HTTP 404 (Sayfa Yok) → {url}")
                return None
            else:
                logger.warning(f"HTTP {r.status_code} → {url}")
        except Exception as e:
            logger.error(f"fetch_html bağlantı hatası [{url}]: {e}")
            await rotate_token()
    return None

async def get_media_name(imdb_id: str, video_type: str) -> str:
    pure_id = imdb_id.split(":")[0]
    try:
        s = await get_session()
        url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
        r = await s.get(url, timeout=5)
        if r.status_code == 200:
            name = r.json().get("meta", {}).get("name", "")
            if name:
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
    return bool(src and (src.startswith("http://") or src.startswith("https://") or src.startswith("//")))

def extract_iframes(soup: BeautifulSoup, source_name: str, emoji: str) -> List[Dict[str, Any]]:
    found = []
    for i, iframe in enumerate(soup.find_all("iframe")):
        src = iframe.get("src", "")
        if is_valid_src(src):
            found.append({
                "name": "TR Addon 🇹🇷",
                "title": f"{source_name} #{i+1} {emoji}",
                "externalUrl": fix_src(src),
            })
    return found

# ─────────────────────────────────────────────────────────
# AKILLI LİNK ANALİZ MOTORU
# ─────────────────────────────────────────────────────────

def get_best_search_result(soup: BeautifulSoup, name: str, video_type: str, domain: str) -> Optional[str]:
    slug = to_slug(name)
    slug_words = slug.split("-")
    best_link = None
    max_score = 0
    
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        
        if any(x in href for x in ["/kategori/", "/oyuncu/", "/etiket/", "/tur/", "/yapim-yili/", "?s="]):
            continue
            
        if video_type == "movie" and ("dizi" in href and "film" not in href):
            continue
        if video_type == "series" and ("film" in href and "dizi" not in href and "bolum" not in href):
            continue
            
        score = sum(1 for word in slug_words if word in href or word in text)
        
        if slug in href:
            score += 10
            
        if score > max_score and score > 0:
            max_score = score
            best_link = a["href"]
            
    if best_link and not best_link.startswith("http"):
        best_link = f"{domain.rstrip('/')}/{best_link.lstrip('/')}"
        
    return best_link

def get_episode_link(soup: BeautifulSoup, domain: str, season: str, episode: str) -> Optional[str]:
    patterns = [
        rf"(?:sezon|s)\s*[-_]?\s*0?{season}\b.*?(?:bolum|bölüm|e)\s*[-_]?\s*0?{episode}\b",
        rf"\b0?{season}\s*[-_]?\s*(?:sezon|s).*?\b0?{episode}\s*[-_]?\s*(?:bolum|bölüm|e)\b",
        rf"\bs0?{season}e0?{episode}\b",
        rf"\b0?{season}x0?{episode}\b"
    ]
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
    
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        
        if "facebook.com" in href or "twitter.com" in href:
            continue
            
        for pattern in compiled_patterns:
            if pattern.search(href) or pattern.search(text):
                link = a["href"]
                if not link.startswith("http"):
                    link = f"{domain.rstrip('/')}/{link.lstrip('/')}"
                return link
    return None

# ─────────────────────────────────────────────────────────
# KÖRSÜZ (NO-GUESS) SCRAPERS
# ─────────────────────────────────────────────────────────

async def scrape_hdfilmcehennemi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    safe_name = urllib.parse.quote_plus(name)
    
    for domain in HDFILM_DOMAINS:
        search_html = await fetch_html(f"{domain}/?s={safe_name}")
        if not search_html: continue
        
        target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
        if not target_url: continue 

        if video_type == "series" and season and episode:
            main_page_html = await fetch_html(target_url)
            if not main_page_html: continue
            
            ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
            if ep_url:
                target_url = ep_url
            else:
                continue 

        page_html = await fetch_html(target_url)
        if page_html:
            found = extract_iframes(BeautifulSoup(page_html, "lxml"), "HDFilmCehennemi", "🔥")
            if found:
                streams.extend(found)
                break
    return streams

async def scrape_fullhdfilmizle(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    safe_name = urllib.parse.quote_plus(name)
    for domain in FULLHDFILM_DOMAINS:
        search_html = await fetch_html(f"{domain}/search?q={safe_name}")
        if not search_html: continue
            
        target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
        if not target_url: continue
            
        page_html = await fetch_html(target_url)
        if page_html:
            found = extract_iframes(BeautifulSoup(page_html, "lxml"), "FullHDFilm", "⚡")
            if found:
                streams.extend(found)
                break
    return streams

async def scrape_dizipal(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    safe_name = urllib.parse.quote_plus(name)
    
    for domain in DIZIPAL_DOMAINS:
        search_html = await fetch_html(f"{domain}/?s={safe_name}")
        if not search_html: continue
        
        target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
        if not target_url: continue

        if video_type == "series" and season and episode:
            if not get_episode_link(BeautifulSoup(f'<a href="{target_url}"></a>', 'lxml'), domain, season, episode):
                main_page_html = await fetch_html(target_url)
                if not main_page_html: continue
                ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
                if ep_url:
                    target_url = ep_url
                else:
                    continue

        page_html = await fetch_html(target_url)
        if page_html:
            found = extract_iframes(BeautifulSoup(page_html, "lxml"), "Dizipal", "📺")
            if found:
                streams.extend(found)
                break
    return streams

async def scrape_yabancidizi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[Dict[str, Any]]:
    streams = []
    safe_name = urllib.parse.quote_plus(name)
    for domain in YABANCIDIZI_DOMAINS:
        search_html = await fetch_html(f"{domain}/arama?q={safe_name}")
        if not search_html: continue
            
        target_url = get_best_search_result(BeautifulSoup(search_html, "lxml"), name, video_type, domain)
        if not target_url: continue
            
        if video_type == "series" and season and episode:
            main_page_html = await fetch_html(target_url)
            if not main_page_html: continue
            
            ep_url = get_episode_link(BeautifulSoup(main_page_html, "lxml"), domain, season, episode)
            if not ep_url: continue
            target_url = ep_url
                
        page_html = await fetch_html(target_url)
        if page_html:
            found = extract_iframes(BeautifulSoup(page_html, "lxml"), "YabanciDizi", "🚀")
            if found:
                streams.extend(found)
                break
    return streams

# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    await get_session()
    if not SCRAPE_DO_TOKENS:
        logger.warning("Hiç SCRAPEDO_KEY tanımlanmamış!")
    else:
        logger.info(f"scrape.do aktif. {len(SCRAPE_DO_TOKENS)} token yüklendi.")
    logger.info("=== TR Sinema Paketi (Sıralı Sabit Motor) Başlatıldı ===")

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scrape_do": f"{len(SCRAPE_DO_TOKENS)} token aktif" if SCRAPE_DO_TOKENS else "TANIMLANMAMIS"
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
    
    # Tam Olarak İstediğin Yapısal Sıralama (Maksimum 3 Kaynak)
    if video_type == "movie":
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("FullHDFilmIzle", scrape_fullhdfilmizle),
            ("YabanciDizi", scrape_yabancidizi)
        ]
    else:  # series
        scraper_pipeline = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi),
            ("Dizipal", scrape_dizipal),
            ("YabanciDizi", scrape_yabancidizi)
        ]

    all_streams: List[dict] = []
    media_name = await get_media_name(imdb_id, video_type)

    # Sırayla ara, bulduğun an kısa devre yap (Maksimum 3 site tarama garantisi)
    for source_name, scraper_func in scraper_pipeline:
        try:
            logger.info(f"Kaynak aranıyor: {source_name}")
            result = await scraper_func(media_name, video_type, season, episode)
            if result:
                logger.info(f"🎉 Yayın {source_name} üzerinde bulundu! Döngü kırılıyor (Sonraki sitelere İSTEK ATILMADI).")
                all_streams.extend(result)
                break  # İçerik bulunduğunda kalan sitelere asla gitmez!
            else:
                logger.info(f"⬜ {source_name} → Sonuç yok, sıradakine geçiliyor.")
        except Exception as e:
            logger.error(f"{source_name} tarama hatası: {e}")

    logger.info(f"Toplam {len(all_streams)} stream donduruluyor.")
    return {"streams": all_streams}
