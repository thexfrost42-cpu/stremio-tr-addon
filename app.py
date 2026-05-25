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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    "description": "Türkçe Dublaj & Altyazılı Film/Dizi — HDFilmCehennemi, FullHDFilmIzle, Dizipal, YabanciDizi",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ── Domainler ─────────────────────────────────────────────
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

TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "25"))

# ── Scrape.do key rotasyonu ────────────────────────────────
_RAW_KEYS = [
    os.getenv("SCRAPEDO_KEY_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
    os.getenv("SCRAPEDO_KEY_2", "ad94f3583fe44d0ca3635c5af37b73f2c64d90c0a59"),
    os.getenv("SCRAPEDO_KEY_3", "c1cb4ed6152049b48307d7e570a485ef66ae1e3b703"),
    os.getenv("SCRAPEDO_KEY_4", "14d6807a8de34c12840670630573ea6596f9036f277"),
]
SCRAPEDO_KEYS = [k for k in _RAW_KEYS if k]
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
        _key_index = (_key_index + 1) % len(SCRAPEDO_KEYS)
        logger.warning(f"Key rotasyonu → {_key_index + 1}. key'e geçildi.")


async def get_active_key() -> str:
    if not SCRAPEDO_KEYS:
        return ""
    return SCRAPEDO_KEYS[_key_index]


# ── HTTP Session ──────────────────────────────────────────
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
    """Scrape.do üzerinden HTML çek. 429/401 gelince key rotasyonu yapar."""
    if not SCRAPEDO_KEYS:
        logger.error("Hiç SCRAPEDO_KEY tanımlanmamış!")
        return None
    try:
        s = await get_session()
        key = await get_active_key()
        encoded = urllib.parse.quote(url, safe="")
        # Scrape.do endpoint — render=true JS render eder, Türkçe siteler için gerekli
        proxy_url = f"https://api.scrape.do?token={key}&url={encoded}&render=true"
        r = await s.get(proxy_url, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
        elif r.status_code in (429, 401, 403):
            logger.warning(f"HTTP {r.status_code} Scrape.do → key rotasyonu yapılıyor...")
            await rotate_key()
            key = await get_active_key()
            proxy_url = f"https://api.scrape.do?token={key}&url={encoded}&render=true"
            r2 = await s.get(proxy_url, timeout=TIMEOUT)
            if r2.status_code == 200:
                return r2.text
            logger.warning(f"Rotasyon sonrası HTTP {r2.status_code} → {url}")
        else:
            logger.warning(f"HTTP {r.status_code} → {url}")
    except Exception as e:
        logger.error(f"fetch_html bağlantı hatası [{url}]: {e}")
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
    return domain.rstrip("/")


# ─────────────────────────────────────────────────────────
# AKILLI İÇERİK LİNKİ BULUCU
# Arama sonucu sayfasından en uygun içerik linkini döndürür.
# URL pattern'ini tahmin etmez — sayfadaki gerçek linkleri kullanır.
# ─────────────────────────────────────────────────────────
def find_best_content_link(
    soup: BeautifulSoup,
    domain: str,
    video_type: str,
    name: str,
) -> Optional[str]:
    """
    Arama sonucu sayfasından içerik linkini bulur.
    İsim benzerliğine göre en iyi eşleşmeyi seçer.
    """
    dom = normalize_domain(domain)
    slug = to_slug(name)
    name_lower = name.lower()

    # video_type'a göre URL pattern
    if video_type == "series":
        url_pattern = re.compile(r"/dizi/|/series/|/show/|/serial/", re.I)
    else:
        url_pattern = re.compile(r"/film/|/movie/|/izle/|/watch/", re.I)

    # Ayrıca genel içerik linkleri (her iki type için)
    general_pattern = re.compile(r"/dizi/|/film/|/izle/|/series/|/movie/|/show/", re.I)

    candidates = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href or href in ("#", "/", "javascript:"):
            continue

        # Tam URL yap
        if href.startswith("http://") or href.startswith("https://"):
            full_href = href
        elif href.startswith("/"):
            full_href = dom + href
        else:
            continue

        # Sadece aynı domain
        if dom.replace("https://", "").replace("http://", "") not in full_href:
            continue

        # Type uyumu kontrolü
        type_match = bool(url_pattern.search(full_href))
        general_match = bool(general_pattern.search(full_href))
        if not general_match:
            continue

        # Slug benzerliği skoru
        href_lower = full_href.lower()
        slug_parts = [p for p in slug.split("-") if len(p) > 2]
        match_score = sum(1 for p in slug_parts if p in href_lower)

        # İsmin kendisi linkte geçiyor mu?
        name_in_href = any(w.lower() in href_lower for w in name_lower.split() if len(w) > 2)

        candidates.append({
            "href": full_href,
            "type_match": type_match,
            "match_score": match_score,
            "name_in_href": name_in_href,
        })

    if not candidates:
        return None

    # Sırala: type_match > name_in_href > match_score
    candidates.sort(key=lambda x: (x["type_match"], x["name_in_href"], x["match_score"]), reverse=True)
    return candidates[0]["href"]


# ─────────────────────────────────────────────────────────
# AKILLI EPISODE BULUCU
# ─────────────────────────────────────────────────────────
def find_episode_link(
    soup: BeautifulSoup,
    domain: str,
    season: str,
    episode: str,
) -> Optional[str]:
    """
    Dizi ana sayfasındaki tüm linkleri tarayarak doğru episode linkini bulur.
    URL pattern'ini tahmin etmez — sayfadaki gerçek linkleri kullanır.

    Desteklenen örüntüler (otomatik algılanır):
      /dizi/xxx/sezon-1/bolum-2/
      /dizi/xxx/sezon-1/bolum-2-hd14/
      /dizi/xxx-izle-157/sezon-1/bolum-1/
      /dizi/xxx/s01e01/
      /season-1/episode-1/
      /1-sezon/2-bolum/
    """
    s_num = int(season)
    e_num = int(episode)
    dom = normalize_domain(domain)
    candidates = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue

        matched = False

        # Pattern 1: sezon-N + bolum-M (Türkçe)
        if re.search(rf"sezon-0?{s_num}\b", href, re.I) and \
           re.search(rf"bolum-0?{e_num}(?:\b|-hd|-[0-9]|/|$)", href, re.I):
            matched = True

        # Pattern 2: s01e01
        elif re.search(rf"s{s_num:02d}e{e_num:02d}", href, re.I):
            matched = True

        # Pattern 3: season-N + episode-M (İngilizce)
        elif re.search(rf"season-0?{s_num}\b", href, re.I) and \
             re.search(rf"episode-0?{e_num}(?:\b|-|/|$)", href, re.I):
            matched = True

        # Pattern 4: /N-sezon/ + /M-bolum/
        elif re.search(rf"/{s_num}-sezon", href, re.I) and \
             re.search(rf"/{e_num}-bolum", href, re.I):
            matched = True

        if matched:
            if href.startswith("http://") or href.startswith("https://"):
                candidates.append(href)
            else:
                candidates.append(dom + "/" + href.lstrip("/"))

    if not candidates:
        return None

    # En spesifik (en uzun, gereksiz # içermeyenler önce)
    candidates = [c for c in candidates if "#" not in c] or candidates
    return max(candidates, key=len)


async def get_iframes_from_page(page_html: str, label: str, emoji: str) -> List[dict]:
    """Sayfadaki geçerli iframe src'lerini döndürür."""
    soup = BeautifulSoup(page_html, "lxml")
    found = []
    for i, iframe in enumerate(soup.find_all("iframe")):
        src = iframe.get("src", "") or iframe.get("data-src", "")
        if is_valid_src(src):
            found.append({
                "name": f"{label} {emoji}",
                "title": f"TR Dublaj/Altyazı #{i+1}",
                "externalUrl": fix_src(src),
            })
    return found


# ─────────────────────────────────────────────────────────
# SCRAPER YARDIMCI: Bir domain için arama → içerik sayfası → iframe
# Maksimum 2 fetch (arama + içerik sayfası) veya 3 fetch (arama + show + episode)
# ─────────────────────────────────────────────────────────

async def _scrape_movie_from_domain(
    dom: str,
    search_url: str,
    name: str,
    label: str,
    emoji: str,
) -> List[dict]:
    """Film için: arama yap → film sayfasını bul → iframe al. Max 2 fetch."""
    search_html = await fetch_html(search_url)
    if not search_html:
        return []

    soup = BeautifulSoup(search_html, "lxml")
    content_href = find_best_content_link(soup, dom, "movie", name)
    if not content_href:
        logger.info(f"⬜ {label} [{dom}] → arama sonucunda içerik linki bulunamadı.")
        return []

    page_html = await fetch_html(content_href)
    if not page_html:
        return []

    found = await get_iframes_from_page(page_html, label, emoji)
    if found:
        logger.info(f"✅ {label} [{dom}] → {len(found)} stream bulundu.")
    else:
        logger.info(f"⬜ {label} [{dom}] → iframe bulunamadı.")
    return found


async def _scrape_series_from_domain(
    dom: str,
    search_url: str,
    name: str,
    season: str,
    episode: str,
    label: str,
    emoji: str,
) -> List[dict]:
    """Dizi için: arama yap → show sayfasını bul → episode linkini tara → iframe al. Max 3 fetch."""
    search_html = await fetch_html(search_url)
    if not search_html:
        return []

    soup = BeautifulSoup(search_html, "lxml")
    show_href = find_best_content_link(soup, dom, "series", name)
    if not show_href:
        logger.info(f"⬜ {label} [{dom}] → arama sonucunda dizi linki bulunamadı.")
        return []

    show_html = await fetch_html(show_href)
    if not show_html:
        return []

    show_soup = BeautifulSoup(show_html, "lxml")
    ep_url = find_episode_link(show_soup, dom, season, episode)

    if ep_url:
        page_html = await fetch_html(ep_url)
        if page_html:
            found = await get_iframes_from_page(page_html, label, emoji)
            if found:
                logger.info(f"✅ {label} [{dom}] → {len(found)} stream bulundu.")
                return found
    
    # Episode linki bulunamadıysa show sayfasını dene
    found = await get_iframes_from_page(show_html, label, emoji)
    if found:
        logger.info(f"✅ {label} [{dom}] (show sayfası) → {len(found)} stream bulundu.")
    else:
        logger.info(f"⬜ {label} [{dom}] → iframe bulunamadı.")
    return found


# ─────────────────────────────────────────────────────────
# SCRAPER FONKSİYONLARI — her biri tek domain'i dener,
# bulursa döner (scrape sayısını minimize eder)
# ─────────────────────────────────────────────────────────

async def scrape_hdfilmcehennemi(name: str, video_type: str, season: Optional[str], episode: Optional[str]) -> List[dict]:
    safe_name = urllib.parse.quote_plus(name)
    for domain in HDFILM_DOMAINS:
        dom = normalize_domain(domain)
        if video_type == "movie":
            found = await _scrape_movie_from_domain(
                dom, f"{dom}/?s={safe_name}", name, "HDFilmCehennemi", "🔥"
            )
        else:
            found = await _scrape_series_from_domain(
                dom, f"{dom}/?s={safe_name}", name, season, episode, "HDFilmCehennemi", "🔥"
            )
        if found:
            return found
    return []


async def scrape_fullhdfilmizle(name: str) -> List[dict]:
    safe_name = urllib.parse.quote_plus(name)
    for domain in FULLHDFILM_DOMAINS:
        dom = normalize_domain(domain)
        found = await _scrape_movie_from_domain(
            dom, f"{dom}/search?q={safe_name}", name, "FullHDFilmIzle", "⚡"
        )
        if found:
            return found
    return []


async def scrape_dizipal(name: str, season: Optional[str], episode: Optional[str]) -> List[dict]:
    if not season or not episode:
        return []
    safe_name = urllib.parse.quote_plus(name)
    slug = to_slug(name)
    for domain in DIZIPAL_DOMAINS:
        dom = normalize_domain(domain)
        # Önce direkt URL dene (1 fetch)
        direct_url = f"{dom}/bolum/{slug}-{season}-sezon-{episode}-bolum"
        page_html = await fetch_html(direct_url)
        if page_html:
            found = await get_iframes_from_page(page_html, "Dizipal", "📺")
            if found:
                logger.info(f"✅ Dizipal [{dom}] direkt URL → {len(found)} stream.")
                return found

        # Direkt tutmadıysa arama yap (2-3 fetch)
        found = await _scrape_series_from_domain(
            dom, f"{dom}/?s={safe_name}", name, season, episode, "Dizipal", "📺"
        )
        if found:
            return found
    return []


async def scrape_yabancidizi(name: str, season: Optional[str], episode: Optional[str]) -> List[dict]:
    if not season or not episode:
        return []
    safe_name = urllib.parse.quote_plus(name)
    for domain in YABANCIDIZI_DOMAINS:
        dom = normalize_domain(domain)
        found = await _scrape_series_from_domain(
            dom, f"{dom}/arama?q={safe_name}", name, season, episode, "YabanciDizi", "🚀"
        )
        if found:
            return found
    return []


# ─────────────────────────────────────────────────────────
# ANA SCRAPE MANTIĞI — SIRASAL, ERKEN ÇIKIŞ
#
# Film sırası:   1. HDFilmCehennemi → 2. FullHDFilmIzle
# Dizi sırası:   1. HDFilmCehennemi → 2. Dizipal → 3. YabanciDizi
#
# Bir kaynak başarılı olursa diğerleri çağrılmaz.
# ─────────────────────────────────────────────────────────

async def find_streams(
    video_type: str,
    name: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:

    if video_type == "movie":
        scrapers = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi(name, "movie", None, None)),
            ("FullHDFilmIzle",  scrape_fullhdfilmizle(name)),
        ]
    else:
        scrapers = [
            ("HDFilmCehennemi", scrape_hdfilmcehennemi(name, "series", season, episode)),
            ("Dizipal",         scrape_dizipal(name, season, episode)),
            ("YabanciDizi",     scrape_yabancidizi(name, season, episode)),
        ]

    for source_name, coro in scrapers:
        logger.info(f"🔍 {source_name} deneniyor...")
        try:
            result = await coro
            if result:
                logger.info(f"✅ {source_name} → {len(result)} stream, diğer kaynaklar atlandı.")
                return result
            else:
                logger.info(f"⬜ {source_name} → bulunamadı, sonraki kaynağa geçiliyor.")
        except Exception as e:
            logger.error(f"❌ {source_name} hata: {e}")

    return []


# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    await get_session()
    if not SCRAPEDO_KEYS:
        logger.warning("⚠️  Hiç SCRAPEDO_KEY tanımlanmamış! render.yaml'a SCRAPEDO_KEY_1 ekle.")
    else:
        logger.info(f"✅ Scrape.do aktif. {len(SCRAPEDO_KEYS)} key yüklendi.")
    logger.info("=== TR Sinema Paketi v12 başlatıldı ===")


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scrape_do": f"{len(SCRAPEDO_KEYS)} key aktif" if SCRAPEDO_KEYS else "⚠️ TANIMLANMAMIŞ",
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    parts = imdb_id.split(":")
    season  = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(f"📥 İstek → type={video_type} id={imdb_id} s={season} e={episode}")

    streams: List[dict] = []
    try:
        name = await get_media_name(imdb_id, video_type)
        streams = await find_streams(video_type, name, season, episode)
    except Exception as e:
        logger.error(f"get_stream genel hatası: {e}")

    if not streams:
        logger.info("❌ Hiçbir kaynaktan stream döndürülemedi.")
    else:
        logger.info(f"📤 Toplam {len(streams)} stream döndürülüyor.")

    return {"streams": streams}
