import asyncio
import logging
import os
import re
import time
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
    "id": "com.tr.turkce.addon.v13",
    "version": "13.0.0",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Önbellek Korumalı (Anti-Spam) & Sabit Kaynaklı Sinema Sağlayıcısı",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ─────────────────────────────────────────────────────────
# SABIT ALAN ADLARI — Google/dinamik arama yok
# ─────────────────────────────────────────────────────────

CURRENT_ACTIVE_DOMAINS: Dict[str, str] = {
    "hdfilmcehennemi": "https://www.hdfilmcehennemi.nl",
    "fullhdfilmizle":  "https://www.fullhdfilmizlesene.life",
    "dizipal":         "https://dizipal2073.com",
    "yabancidizi":     "https://yabancidizi.life",
}

# ─────────────────────────────────────────────────────────
# IN-MEMORY CACHE — Stremio spam koruması
# ─────────────────────────────────────────────────────────

STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600  # saniye (1 saat)

# ─────────────────────────────────────────────────────────
# SCRAPE.DO TOKEN YÖNETİMİ
# ─────────────────────────────────────────────────────────

REQUEST_TIMEOUT = 5  # Render & Stremio toleransı için katı üst sınır

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
    if not SCRAPE_DO_TOKENS:
        return ""
    return SCRAPE_DO_TOKENS[_token_index]


def rotate_token() -> None:
    global _token_index
    if not SCRAPE_DO_TOKENS:
        return
    _token_index = (_token_index + 1) % len(SCRAPE_DO_TOKENS)
    logger.warning(f"🔄 Token rotasyonu yapıldı: {_token_index + 1}. token devrede.")


# ─────────────────────────────────────────────────────────
# HTTP KATMANI — Render shutdown koruması
# Her istek bağımsız Session ile thread'de çalışır.
# ─────────────────────────────────────────────────────────

def sync_fetch_html(url: str) -> Optional[str]:
    """
    Bloklayıcı HTTP isteği — asyncio.to_thread() ile çağrılmalı.
    curl_cffi'nin ana event-loop'u dondurmasını engeller.
    Global session yasak: her çağrıda bağımsız Session aç/kapat.
    """
    token = get_active_token()
    if not token:
        logger.warning("⚠️ Aktif scrape.do tokeni yok, istek atılamıyor.")
        return None

    encoded_target = urllib.parse.quote(url, safe="")
    proxy_url = f"https://api.scrape.do?token={token}&url={encoded_target}"

    try:
        # impersonate='chrome120' zorunlu; with-bloğu Session'ı her zaman kapatır
        with requests.Session(impersonate="chrome120") as s:
            r = s.get(proxy_url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403, 429):
                rotate_token()
            logger.warning(f"⚠️ HTTP {r.status_code} — {url}")
            return None
    except Exception as e:
        logger.error(f"⚠️ Proxy bağlantı hatası [{url}]: {e}")
        return None


async def fetch_html(url: str) -> Optional[str]:
    """Bloklayıcı isteği arka plana taşır, event-loop'u serbest bırakır."""
    return await asyncio.to_thread(sync_fetch_html, url)


# ─────────────────────────────────────────────────────────
# METADATA
# ─────────────────────────────────────────────────────────

# Cinemeta'nın eklediği parantez etiketlerini temizleyen regex
_CINEMETA_SUFFIX_RE = re.compile(r"\s*\((movie|series|show)\)\s*$", re.IGNORECASE)


def get_cinemeta_sync(imdb_id: str, video_type: str) -> str:
    """Cinemeta'dan medya adını çeker; hata halinde ham IMDB id'yi döner."""
    pure_id = imdb_id.split(":")[0]
    url = f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json"
    try:
        with requests.Session() as s:
            r = s.get(url, timeout=3)
            if r.status_code == 200:
                name: str = r.json().get("meta", {}).get("name", "")
                if name:
                    # "(movie)", "(series)", "(show)" gibi ekleri temizle
                    name = _CINEMETA_SUFFIX_RE.sub("", name).strip()
                    return name
    except Exception as e:
        logger.error(f"⚠️ Cinemeta hatası [{pure_id}]: {e}")
    return pure_id


async def get_media_name(imdb_id: str, video_type: str) -> str:
    return await asyncio.to_thread(get_cinemeta_sync, imdb_id, video_type)


# ─────────────────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────────────────

def to_slug(text: str) -> str:
    """Türkçe karakterleri ASCII'ye çevirip URL-dostu slug üretir."""
    if not text:
        return ""
    tr_map = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisoucgisou")
    text = text.translate(tr_map)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", text.strip()))


def detect_player_provider(url: str) -> str:
    """iframe URL'inden oynatıcı adını/etiketini tespit eder."""
    u = url.lower()
    if "vidmoly"      in u: return "Vidmoly 🟣"
    if "fembed"       in u or "feurl"         in u: return "Fembed 🟠"
    if "ok.ru"        in u or "odnoklassniki" in u: return "OK.ru 🟤"
    if "vidoza"       in u: return "Vidoza 🟢"
    if "streamtape"   in u: return "Streamtape 🔵"
    if "voe.sx"       in u: return "VOE 🟡"
    if "dood"         in u: return "DoodStream 🔴"
    if "plusplayer"   in u or "moly"          in u: return "Hızlı Player ⚡"
    return "Yayın Sunucusu 🌐"


def extract_iframes(
    soup: BeautifulSoup,
    source_name: str,
    emoji: str,
) -> List[Dict[str, Any]]:
    """Sayfadaki tüm iframe'leri Stremio stream nesnesine dönüştürür."""
    found: List[Dict[str, Any]] = []
    for iframe in soup.find_all("iframe"):
        src: str = iframe.get("src", "").strip()
        if src.startswith("//"):
            src = "https:" + src
        if src and (src.startswith("http://") or src.startswith("https://")):
            provider_title = detect_player_provider(src)
            found.append({
                "name": "TR Addon 🇹🇷",
                "title": f"{source_name} ➔ {provider_title} {emoji}",
                "externalUrl": src,
            })
    return found


def get_best_search_result(
    soup: BeautifulSoup,
    name: str,
    video_type: str,
    domain: str,
) -> Optional[str]:
    """Arama sonuç sayfasında en yüksek skorlu linki döner."""
    target_slug  = to_slug(name)
    target_words = [w for w in target_slug.split("-") if len(w) > 1] or [target_slug]

    best_link: Optional[str] = None
    max_score = 0

    for a in soup.find_all("a", href=True):
        href      = a["href"].lower()
        text_slug = to_slug(a.get_text(strip=True))

        # Filtre: kategori/etiket/sayfalama linkleri
        if any(x in href for x in [
            "/kategori/", "/oyuncu/", "/etiket/",
            "/tur/", "/yapim-yili/", "?s=", "/page/",
        ]):
            continue

        # Filtre: yanlış içerik türü
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


def get_episode_link(
    soup: BeautifulSoup,
    domain: str,
    season: str,
    episode: str,
) -> Optional[str]:
    """Dizi sayfasında ilgili sezon/bölüm linkini bulur."""
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
# SCRAPER FONKSİYONLARI — Her biri izole try-except ile
# ─────────────────────────────────────────────────────────

async def scrape_hdfilmcehennemi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[Dict[str, Any]]:
    streams: List[Dict[str, Any]] = []
    domain = CURRENT_ACTIVE_DOMAINS["hdfilmcehennemi"]

    search_html = await fetch_html(f"{domain}/?s={urllib.parse.quote_plus(name)}")
    if not search_html:
        return streams

    target_url = get_best_search_result(
        BeautifulSoup(search_html, "lxml"), name, video_type, domain
    )
    if not target_url:
        return streams

    if video_type == "series" and season and episode:
        main_html = await fetch_html(target_url)
        if not main_html:
            return streams
        ep_url = get_episode_link(BeautifulSoup(main_html, "lxml"), domain, season, episode)
        if not ep_url:
            return streams
        target_url = ep_url

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "HDFilmCehennemi", "🔥"))
    return streams


async def scrape_fullhdfilmizle(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[Dict[str, Any]]:
    streams: List[Dict[str, Any]] = []
    domain = CURRENT_ACTIVE_DOMAINS["fullhdfilmizle"]

    search_html = await fetch_html(f"{domain}/search?q={urllib.parse.quote_plus(name)}")
    if not search_html:
        return streams

    target_url = get_best_search_result(
        BeautifulSoup(search_html, "lxml"), name, video_type, domain
    )
    if not target_url:
        return streams

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "FullHDFilm", "⚡"))
    return streams


async def scrape_dizipal(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[Dict[str, Any]]:
    streams: List[Dict[str, Any]] = []
    domain = CURRENT_ACTIVE_DOMAINS["dizipal"]

    search_html = await fetch_html(f"{domain}/?s={urllib.parse.quote_plus(name)}")
    if not search_html:
        return streams

    target_url = get_best_search_result(
        BeautifulSoup(search_html, "lxml"), name, video_type, domain
    )
    if not target_url:
        return streams

    if video_type == "series" and season and episode:
        main_html = await fetch_html(target_url)
        if not main_html:
            return streams
        ep_url = get_episode_link(BeautifulSoup(main_html, "lxml"), domain, season, episode)
        if not ep_url:
            return streams
        target_url = ep_url

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "Dizipal", "📺"))
    return streams


async def scrape_yabancidizi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[Dict[str, Any]]:
    streams: List[Dict[str, Any]] = []
    domain = CURRENT_ACTIVE_DOMAINS["yabancidizi"]

    search_html = await fetch_html(f"{domain}/arama?q={urllib.parse.quote_plus(name)}")
    if not search_html:
        return streams

    target_url = get_best_search_result(
        BeautifulSoup(search_html, "lxml"), name, video_type, domain
    )
    if not target_url:
        return streams

    if video_type == "series" and season and episode:
        main_html = await fetch_html(target_url)
        if not main_html:
            return streams
        ep_url = get_episode_link(BeautifulSoup(main_html, "lxml"), domain, season, episode)
        if not ep_url:
            return streams
        target_url = ep_url

    page_html = await fetch_html(target_url)
    if page_html:
        streams.extend(extract_iframes(BeautifulSoup(page_html, "lxml"), "YabanciDizi", "🚀"))
    return streams


# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.get("/")
def health_check() -> Dict[str, Any]:
    return {
        "status":        "ok",
        "addon":         MANIFEST["name"],
        "cache_size":    len(STREAM_CACHE),
        "tokens_active": len(SCRAPE_DO_TOKENS) > 0,
    }


@app.get("/manifest.json")
def get_manifest() -> Dict[str, Any]:
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str) -> Dict[str, Any]:
    cache_key    = f"{video_type}_{imdb_id}"
    current_time = time.time()

    # ── Önbellek kontrolü — 0 kredi harcatır ──────────────
    if cache_key in STREAM_CACHE:
        cached = STREAM_CACHE[cache_key]
        if current_time - cached["timestamp"] < CACHE_TTL:
            logger.info(f"⚡ Önbellekten yüklendi: {imdb_id} (0 Kredi)")
            return {"streams": cached["streams"]}
        del STREAM_CACHE[cache_key]  # Süresi dolmuş kaydı temizle

    # ── Sezon/Bölüm ayrıştırması ──────────────────────────
    parts   = imdb_id.split(":")
    season  = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode = parts[2] if len(parts) > 2 and video_type == "series" else None

    # ── Pipeline tanımı — asyncio.gather KULLANILMAZ ──────
    # İlk başarılı kaynakta döngü kırılır (Short-Circuit).
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
                all_streams.extend(result)
                break  # Kredi koruma: diğer sitelere istek atma
        except Exception as e:
            logger.error(f"❌ {source_name} tarama hatası: {e}")

    # ── Sonucu önbelleğe yaz ──────────────────────────────
    STREAM_CACHE[cache_key] = {
        "timestamp": current_time,
        "streams":   all_streams,
    }

    return {"streams": all_streams}
