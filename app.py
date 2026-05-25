import asyncio
import json
import logging
import os
import re
import unicodedata
import urllib.parse
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Manifest ─────────────────────────────────────────────────────────────────
MANIFEST = {
    "id": "com.tr.turkce.addon.v12",
    "version": "1.12.4",
    "name": "TR Sinema Paketi 🇹🇷",
    "description": "Türkçe Dublaj Film & Dizi — HDFilmCehennemi, FullHDFilmIzle, Dizipal, YabanciDizi",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Play_icon_red.svg/512px-Play_icon_red.svg.png",
    "background": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070",
    "behaviorHints": {"configurable": False, "adult": False},
}

# ─── Domain Listeleri ─────────────────────────────────────────────────────────
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
    "https://www.hdfilmcehennemi.nl",
    "https://www.hdfilmcehennemi.life",
    "https://www.hdfilmcehennemi.net",
]
FULLHDFILM_DOMAINS = [
    "https://www.fullhdfilmizlesene.de",
    "https://www.fullhdfilmizlesene.com",
    "https://www.fullhdfilmizlesene.pw",
]

# ─── Config ───────────────────────────────────────────────────────────────────
TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "15"))

# ─── scrape.do Token Yönetimi ─────────────────────────────────────────────────
SCRAPER_API_KEYS: List[str] = [
    k
    for k in [
        os.getenv("SCRAPE_DO_TOKEN_1", "a07f9a13f00041c28a4d8f51b201a1e93f1a78a9fea"),
        os.getenv("SCRAPE_DO_TOKEN_2", ""),
        os.getenv("SCRAPE_DO_TOKEN_3", ""),
        os.getenv("SCRAPE_DO_TOKEN_4", ""),
    ]
    if k
]
_key_index: int = 0
_key_lock: Optional[asyncio.Lock] = None


def _get_key_lock() -> asyncio.Lock:
    global _key_lock
    if _key_lock is None:
        _key_lock = asyncio.Lock()
    return _key_lock


def _current_key() -> str:
    return SCRAPER_API_KEYS[_key_index] if SCRAPER_API_KEYS else ""


async def _rotate_key() -> None:
    global _key_index
    async with _get_key_lock():
        _key_index = (_key_index + 1) % len(SCRAPER_API_KEYS)
    logger.warning(f"Token rotasyonu → {_key_index + 1}. token aktif")


# ─── HTTP Session ─────────────────────────────────────────────────────────────
_session: Optional[requests.AsyncSession] = None
_session_lock: Optional[asyncio.Lock] = None


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def get_session() -> requests.AsyncSession:
    global _session
    async with _get_session_lock():
        if _session is None:
            _session = requests.AsyncSession(impersonate="chrome120")
            logger.info("AsyncSession oluşturuldu.")
    return _session


# ─── Domain Sağlık Cache'i ────────────────────────────────────────────────────
# Son başarılı domain'in indeksini tutar → gereksiz denemeyi önler
_domain_cache: Dict[str, int] = {
    "dizipal": 0,
    "yabancidizi": 0,
    "hdfilm": 0,
    "fullhdfilm": 0,
}


def get_domains_ordered(site: str, domain_list: List[str]) -> List[str]:
    """Son çalışan domain'i başa alarak döner; ölü domainleri sona bırakır."""
    idx = _domain_cache.get(site, 0)
    return domain_list[idx:] + domain_list[:idx]


def mark_domain_working(site: str, domain: str, domain_list: List[str]) -> None:
    """Başarılı domain'i cache'e yazar."""
    try:
        _domain_cache[site] = domain_list.index(domain)
    except ValueError:
        pass


# ─── HTTP Fetch ───────────────────────────────────────────────────────────────
async def fetch_html(url: str) -> Optional[str]:
    """
    scrape.do proxy üzerinden URL çeker.
    429 / 403 alınırsa token rotasyonu yapar (max 2 deneme).
    """
    if not SCRAPER_API_KEYS:
        logger.error("Hiç SCRAPE_DO_TOKEN tanımlanmamış!")
        return None

    s = await get_session()
    encoded = urllib.parse.quote(url, safe="")
    max_attempts = min(2, len(SCRAPER_API_KEYS))

    for attempt in range(max_attempts):
        token = _current_key()
        proxy_url = f"https://api.scrape.do?token={token}&url={encoded}"
        try:
            r = await s.get(proxy_url, timeout=TIMEOUT)
        except Exception as e:
            logger.error(f"fetch_html bağlantı hatası [{url}]: {e}")
            return None

        if r.status_code == 200:
            return r.text

        if r.status_code in (429, 403):
            logger.warning(f"HTTP {r.status_code} (deneme {attempt + 1}) → token rotasyonu")
            await _rotate_key()
            continue

        logger.warning(f"HTTP {r.status_code} → {url}")
        return None

    return None


# ─── Cinemeta ─────────────────────────────────────────────────────────────────
async def get_media_name(imdb_id: str, video_type: str) -> str:
    """Cinemeta'dan film/dizi adını çeker. scrape.do kullanmaz."""
    pure_id = imdb_id.split(":")[0]
    try:
        s = await get_session()
        r = await s.get(
            f"https://v3-cinemeta.strem.io/meta/{video_type}/{pure_id}.json",
            timeout=8,
        )
        if r.status_code == 200:
            name = r.json().get("meta", {}).get("name", "")
            if name:
                logger.info(f"Cinemeta adı: {name}")
                return name
    except Exception as e:
        logger.warning(f"Cinemeta hatası: {e}")
    return pure_id


# ─── Yardımcı Araçlar ─────────────────────────────────────────────────────────
def to_slug(text: str) -> str:
    """Türkçe dahil metni URL-uyumlu slug'a dönüştürür."""
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
    return "https:" + src if src.startswith("//") else src


def is_valid_src(src: str) -> bool:
    return bool(src and src.startswith(("http://", "https://", "//")))


def has_video_content(html: str) -> bool:
    """
    Sayfanın gerçek bir video/player sayfası olup olmadığını hızla kontrol eder.
    404/hata sayfalarını ve boş cevapları elek → gereksiz parse'ı önler.
    """
    if not html or len(html) < 800:
        return False
    lower = html.lower()
    # 404 / hata sayfası işaretleri
    error_signals = [
        "sayfa bulunamadı",
        "sayfa bulunamadi",
        "404 not found",
        "page not found",
        "hata 404",
        "bulunamadı",
    ]
    if any(s in lower for s in error_signals):
        return False
    # Video içeriği işaretleri
    return any(s in lower for s in ["iframe", "<video", "player", "embed"])


def slug_matches(slug: str, text: str) -> bool:
    """
    Slug kelimelerinin yeterince text içinde geçip geçmediğini kontrol eder.
    HDFilmCehennemi gibi siteler slug'a yıl ve ID ekler (euphoria-2019-izle-157),
    bu yüzden tam eşleşme yerine kelime örtüşmesi kullanılır.
    """
    words = [w for w in slug.split("-") if len(w) > 2]
    if not words:
        return True  # çok kısa slug → eşleşti say
    text_lower = text.lower()
    match_count = sum(1 for w in words if w in text_lower)
    return match_count >= max(1, len(words) // 2)


def extract_dublaj_iframes(soup: BeautifulSoup) -> List[str]:
    """
    Sayfadan Türkçe Dublaj iframe src'lerini çıkarır.
    Öncelik: dublaj container → dublaj metin yakını → tüm iframe'ler (fallback)
    """
    seen: set = set()
    iframes: List[str] = []

    def add(src: str) -> None:
        fsrc = fix_src(src)
        if fsrc not in seen:
            seen.add(fsrc)
            iframes.append(fsrc)

    # Strateji 1: id veya class'ında 'dublaj' geçen kapsayıcılar
    containers = soup.find_all(
        True, attrs={"id": re.compile(r"dublaj", re.I)}
    ) + soup.find_all(True, attrs={"class": re.compile(r"dublaj", re.I)})

    for container in containers:
        for iframe in container.find_all("iframe"):
            src = iframe.get("src", "")
            if is_valid_src(src):
                add(src)
    if iframes:
        return iframes

    # Strateji 2: "türkçe dublaj" metni yakınındaki iframe'ler
    for text_node in soup.find_all(string=re.compile(r"t[uü]rk[cç]e\s*dublaj", re.I)):
        parent = text_node.parent
        for _ in range(7):
            if parent is None:
                break
            for iframe in parent.find_all("iframe"):
                src = iframe.get("src", "")
                if is_valid_src(src):
                    add(src)
            if iframes:
                break
            parent = parent.parent
    if iframes:
        return iframes

    # Strateji 3 (fallback): sayfadaki tüm iframe'ler
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if is_valid_src(src):
            add(src)
    return iframes


def make_streams(srcs: List[str], label: str) -> List[dict]:
    return [
        {
            "name": "TR Addon 🇹🇷",
            "title": f"{label} #{i + 1}\n🇹🇷 Türkçe Dublaj",
            "externalUrl": src,
        }
        for i, src in enumerate(srcs)
    ]


async def _fetch_and_extract(url: str) -> List[str]:
    """
    URL'yi çek → video içeriği varsa dublaj iframe'lerini döndür.
    has_video_content() ile 404/hata sayfalarını parse etmeden elek.
    """
    html = await fetch_html(url)
    if not html or not has_video_content(html):
        return []
    return extract_dublaj_iframes(BeautifulSoup(html, "lxml"))


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 1 – DİZİPAL
#
# URL Yapısı:
#   Dizi : {domain}/bolum/{slug}-{season}-sezon-{episode}-bolum/
#   Film : {domain}/film/{slug}-izle/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_dizipal(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if not name:
        return []
    if video_type == "series" and not (season and episode):
        return []

    slug = to_slug(name)

    for domain in get_domains_ordered("dizipal", DIZIPAL_DOMAINS):
        url = (
            f"{domain}/bolum/{slug}-{season}-sezon-{episode}-bolum/"
            if video_type == "series"
            else f"{domain}/film/{slug}-izle/"
        )
        srcs = await _fetch_and_extract(url)
        if srcs:
            mark_domain_working("dizipal", domain, DIZIPAL_DOMAINS)
            logger.info(f"Dizipal: {len(srcs)} stream [{domain}]")
            return make_streams(srcs, "Dizipal 📺")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 2 – YABANCI DİZİ  (yalnızca dizi)
#
# URL Yapısı:
#   Dizi : {domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_yabancidizi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if video_type != "series" or not name or not season or not episode:
        return []

    slug = to_slug(name)

    for domain in get_domains_ordered("yabancidizi", YABANCIDIZI_DOMAINS):
        url = f"{domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/"
        srcs = await _fetch_and_extract(url)
        if srcs:
            mark_domain_working("yabancidizi", domain, YABANCIDIZI_DOMAINS)
            logger.info(f"YabanciDizi: {len(srcs)} stream [{domain}]")
            return make_streams(srcs, "YabanciDizi 🚀")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 3 – HD FİLM CEHENNEMİ  (film + dizi)
#
# Bu site değişken URL yapısı kullanır:
#
#   Film örnekleri:
#     /baba-beni-guldursene/               ← sadece slug, /film/ prefix'i yok
#     /{slug}-izle/
#     /film/{slug}-izle/
#
#   Dizi örnekleri:
#     /dizi/everything-now/sezon-1/bolum-1/
#     /dizi/euphoria-2019-izle-157/sezon-1/bolum-1-hd14/
#     /dizi/stranger-things-tales-from-85/
#
# Strateji (Film):
#   1. Doğrudan URL kalıpları dene (/{slug}-izle/, /{slug}/, /film/{slug}-izle/)
#   2. Başarısız → site içi arama yap, sonuçtan URL bul
#
# Strateji (Dizi):
#   1. Doğrudan URL dene (/dizi/{slug}/sezon-{S}/bolum-{E}/)
#   2. Başarısız → site içi arama ile show kök URL'sini bul
#   3a. Show kökünden sezon/bölüm URL'sini tahmin et
#   3b. Tahmin başarısız → show sayfasını parse et, link bul
# ─────────────────────────────────────────────────────────────────────────────
async def _hdfilm_search(
    domain: str, name: str, content_type: str
) -> Optional[str]:
    """
    Site içi WordPress araması ile doğru içerik URL'sini bulur.

    content_type='series' → dizi kök URL'si döner: {domain}/dizi/{show-slug}/
    content_type='movie'  → film sayfasının tam URL'sini döner
    """
    search_url = f"{domain}/?s={urllib.parse.quote(name)}"
    html = await fetch_html(search_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    slug = to_slug(name)

    # Domain'e ait tüm linkleri topla
    all_links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(domain):
            all_links.append(href)

    if content_type == "series":
        # --- Geçiş 1: /dizi/{show-slug}/ formatında kök URL ---
        for href in all_links:
            path = href[len(domain):].strip("/")
            m = re.match(r"^dizi/([^/]+)$", path)
            if m and slug_matches(slug, m.group(1)):
                return f"{domain}/dizi/{m.group(1)}/"

        # --- Geçiş 2: Bölüm linkinden show kökünü çıkar ---
        # (Arama bazen direkt bölüm sonuçları listeler)
        for href in all_links:
            path = href[len(domain):].strip("/")
            m = re.match(r"^dizi/([^/]+)/", path)
            if m and slug_matches(slug, m.group(1)):
                return f"{domain}/dizi/{m.group(1)}/"

    else:  # movie
        for href in all_links:
            path = href[len(domain):].strip("/").split("?")[0]
            # Dizi yolu içermemeli
            if "dizi" not in path and "sezon" not in path and "bolum" not in path:
                if slug_matches(slug, path):
                    return href

    return None


async def _hdfilm_find_episode_url(
    show_url: str, season: str, episode: str
) -> Optional[str]:
    """
    Dizi ana sayfasını parse ederek belirtilen sezon/bölüm linkini döner.
    Hem /bolum-1/ hem /bolum-1-hd14/ formatını doğru eşler;
    bolum-1 ile bolum-10'u karıştırmaz.
    """
    html = await fetch_html(show_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    # Kesin sezon/bölüm eşleşmesi — sayı sınırı kontrolü dahil
    s_pat = re.compile(rf"/sezon-{re.escape(season)}/", re.I)
    e_pat = re.compile(rf"/bolum-{re.escape(episode)}([^0-9]|$)", re.I)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if s_pat.search(href) and e_pat.search(href):
            if href.startswith("//"):
                return "https:" + href
            if href.startswith("http"):
                return href

    return None


async def scrape_hdfilmcehennemi(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if not name:
        return []

    slug = to_slug(name)

    for domain in get_domains_ordered("hdfilm", HDFILM_DOMAINS):
        if video_type == "movie":
            # ── Film: Doğrudan URL kalıpları ──────────────────────────────
            # Site farklı film path'leri kullanabilir; hepsini sırayla dene
            for url in [
                f"{domain}/{slug}-izle/",
                f"{domain}/{slug}/",
                f"{domain}/film/{slug}-izle/",
            ]:
                srcs = await _fetch_and_extract(url)
                if srcs:
                    mark_domain_working("hdfilm", domain, HDFILM_DOMAINS)
                    logger.info(f"HDFilmCehennemi film (direkt): {len(srcs)} stream [{url}]")
                    return make_streams(srcs, "HDFilmCehennemi 🔥")

            # ── Film: Site araması (fallback) ──────────────────────────────
            film_url = await _hdfilm_search(domain, name, "movie")
            if film_url:
                srcs = await _fetch_and_extract(film_url)
                if srcs:
                    mark_domain_working("hdfilm", domain, HDFILM_DOMAINS)
                    logger.info(f"HDFilmCehennemi film (arama): {len(srcs)} stream [{film_url}]")
                    return make_streams(srcs, "HDFilmCehennemi 🔥")

        else:  # series
            if not (season and episode):
                return []

            # ── Dizi: Doğrudan basit URL dene ─────────────────────────────
            # "everything-now" gibi sade slug'lu diziler için çalışır
            direct_url = f"{domain}/dizi/{slug}/sezon-{season}/bolum-{episode}/"
            srcs = await _fetch_and_extract(direct_url)
            if srcs:
                mark_domain_working("hdfilm", domain, HDFILM_DOMAINS)
                logger.info(f"HDFilmCehennemi dizi (direkt): {len(srcs)} stream")
                return make_streams(srcs, "HDFilmCehennemi 🔥")

            # ── Dizi: Site araması ile show URL'sini bul ───────────────────
            show_url = await _hdfilm_search(domain, name, "series")
            if not show_url:
                continue  # Bu domain'de bulunamadı, sonraki domain'e geç

            # ── Dizi: Bölüm URL'sini tahmin et (hızlı yol) ─────────────────
            # "euphoria-2019-izle-157" gibi show slug'unu biliyoruz artık
            ep_url_guess = f"{show_url}sezon-{season}/bolum-{episode}/"
            srcs = await _fetch_and_extract(ep_url_guess)
            if srcs:
                mark_domain_working("hdfilm", domain, HDFILM_DOMAINS)
                logger.info(f"HDFilmCehennemi dizi (arama+tahmin): {len(srcs)} stream")
                return make_streams(srcs, "HDFilmCehennemi 🔥")

            # ── Dizi: Show sayfasını parse et, bölüm linkini bul ───────────
            # "-hd14" gibi suffix'li bölümler için güvenli yol
            ep_url = await _hdfilm_find_episode_url(show_url, season, episode)
            if ep_url:
                srcs = await _fetch_and_extract(ep_url)
                if srcs:
                    mark_domain_working("hdfilm", domain, HDFILM_DOMAINS)
                    logger.info(f"HDFilmCehennemi dizi (arama+parse): {len(srcs)} stream")
                    return make_streams(srcs, "HDFilmCehennemi 🔥")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER 4 – FULLHD FİLM İZLE  (yalnızca film)
#
# URL Yapısı:
#   Film : {domain}/{slug}-izle/
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_fullhdfilmizle(
    name: str,
    video_type: str,
    season: Optional[str],
    episode: Optional[str],
) -> List[dict]:
    if video_type != "movie" or not name:
        return []

    slug = to_slug(name)

    for domain in get_domains_ordered("fullhdfilm", FULLHDFILM_DOMAINS):
        url = f"{domain}/{slug}-izle/"
        srcs = await _fetch_and_extract(url)
        if srcs:
            mark_domain_working("fullhdfilm", domain, FULLHDFILM_DOMAINS)
            logger.info(f"FullHDFilmIzle: {len(srcs)} stream [{domain}]")
            return make_streams(srcs, "FullHDFilm ⚡")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# APP LIFECYCLE & ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    await get_session()
    if not SCRAPER_API_KEYS:
        logger.warning("Hiç SCRAPE_DO_TOKEN tanımlanmamış!")
    else:
        logger.info(f"scrape.do aktif. {len(SCRAPER_API_KEYS)} token yüklendi.")
    logger.info("=== TR Sinema Paketi v1.12.4 başlatıldı ===")
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "addon": MANIFEST["name"],
        "version": MANIFEST["version"],
        "scrape_do": (
            f"{len(SCRAPER_API_KEYS)} token aktif"
            if SCRAPER_API_KEYS
            else "TANIMLANMAMIŞ"
        ),
    }


@app.get("/manifest.json")
def get_manifest():
    return MANIFEST


@app.get("/stream/{video_type}/{imdb_id}.json")
async def get_stream(video_type: str, imdb_id: str):
    """
    imdb_id formatı:
      Film : tt1234567
      Dizi : tt1234567:1:2  (id:sezon:bolum)

    Pipeline — Film : HDFilmCehennemi → FullHDFilmIzle → Dizipal
    Pipeline — Dizi : HDFilmCehennemi → Dizipal → YabanciDizi

    Her scraper ilk başarıda durur → gereksiz scrape.do kredisi harcanmaz.
    Domain sağlık cache'i sayesinde ölü domainler sona bırakılır.
    """
    parts = imdb_id.split(":")
    season: Optional[str] = parts[1] if len(parts) > 1 and video_type == "series" else None
    episode: Optional[str] = parts[2] if len(parts) > 2 and video_type == "series" else None

    logger.info(f"İstek → type={video_type} id={imdb_id} s={season} e={episode}")

    try:
        name = await get_media_name(imdb_id, video_type)

        if video_type == "movie":
            pipeline = [
                ("HDFilmCehennemi", scrape_hdfilmcehennemi),
                ("FullHDFilmIzle",  scrape_fullhdfilmizle),
                ("Dizipal",         scrape_dizipal),
            ]
        else:
            pipeline = [
                ("HDFilmCehennemi", scrape_hdfilmcehennemi),
                ("Dizipal",         scrape_dizipal),
                ("YabanciDizi",     scrape_yabancidizi),
            ]

        for source_name, scraper_fn in pipeline:
            try:
                streams = await scraper_fn(name, video_type, season, episode)
            except Exception as e:
                logger.error(f"{source_name} scraper hatası: {e}")
                streams = []

            if streams:
                logger.info(f"✅ {source_name} → {len(streams)} stream döndürüldü.")
                return {"streams": streams}

            logger.info(f"⬜ {source_name} → sonuç yok, sonraki kaynağa geçiliyor.")

    except Exception as e:
        logger.error(f"get_stream genel hatası: {e}")

    logger.info("Hiçbir kaynaktan stream döndürülemedi.")
    return {"streams": []}
