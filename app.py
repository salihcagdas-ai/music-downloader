"""
Local YouTube / Facebook downloader.

Tek bilgisayarında (localhost) çalışan küçük bir web uygulaması.
Arayüze linkleri yapıştırırsın, arkada yt-dlp indirir.
Dışarıya (ağa) HİÇ açılmaz — sadece 127.0.0.1'e bağlanır.
"""

import os
import re
import sys
import shutil
import threading
import uuid
import subprocess
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template

import yt_dlp

# --- Ayarlar --------------------------------------------------------------

# PyInstaller ile tek .exe olarak paketlenince (Windows) çalışma şekli değişir:
#   - FROZEN True olur.
#   - Gömülü dosyalar (templates, ffmpeg) geçici bir klasöre (sys._MEIPASS) açılır
#     — burası SALT OKUNUR ve uygulama kapanınca SİLİNİR.
#   - O yüzden indirilenleri buraya değil, .exe'nin YANINA yazmalıyız.
FROZEN = getattr(sys, "frozen", False)

# RESOURCE_DIR: gömülü/okunur dosyalar (templates, gömülü ffmpeg).
RESOURCE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
# BASE_DIR: yazılabilir dosyalar (downloads, arşiv). exe ise exe'nin yanı.
if FROZEN:
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
# Arşivi FORMATA GÖRE ayrı tutuyoruz. Aksi halde bir videoyu mp3 olarak
# indirince, aynı videoyu mp4 isteyince "zaten var" deyip atlardı.
ARCHIVE_AUDIO = os.path.join(BASE_DIR, "archive-audio.txt")
ARCHIVE_VIDEO = os.path.join(BASE_DIR, "archive-video.txt")

# ffmpeg'i güvenilir şekilde bul (mp3'e çevirmek / mp4 birleştirmek için şart).
#   1) Windows .exe: ffmpeg gömülü gelir → RESOURCE_DIR içinde ara.
#   2) PATH'te varsa (shutil.which) — Finder'dan açılınca PATH dar olabilir.
#   3) Mac (brew) yaygın konumları / Windows yaygın konumları.
def _find_ffmpeg():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [
        os.path.join(RESOURCE_DIR, exe),          # gömülü (PyInstaller)
        os.path.join(BASE_DIR, exe),              # exe'nin yanı
        shutil.which("ffmpeg"),                   # PATH
        "/opt/homebrew/bin/ffmpeg",               # Mac Apple Silicon (brew)
        "/usr/local/bin/ffmpeg",                  # Mac Intel (brew)
        r"C:\ffmpeg\bin\ffmpeg.exe",              # Windows yaygın
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None

FFMPEG = _find_ffmpeg()

# Aynı anda kaç indirme yapılsın
MAX_CONCURRENT = 3

# Bellekte tutulacak en fazla iş sayısı (uzun oturumda şişmeyi engeller).
# Sınır aşılınca en eski BİTMİŞ/HATALI işler listeden düşer (dosyalara dokunulmaz).
MAX_JOBS = 200

# Dosya adı şablonu.
#   [%(id)s]  → videonun benzersiz kimliği; aynı başlıklı FARKLI videoların
#               birbirini sessizce ezmesini (veri kaybı) engeller.
#   .180B     → başlığı 180 bayta kırpar; çok uzun / emoji / CJK başlıkların
#               macOS'ta "dosya adı çok uzun" hatası vermesini önler.
# Kimliksiz TEMİZ isim istersen:  "%(title).180B.%(ext)s"
# (ama nadiren aynı isimli iki farklı video çakışabilir.)
FILENAME_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# template_folder'ı açıkça veriyoruz: exe olarak paketlenince şablonlar
# RESOURCE_DIR (gömülü) içinde olur; normal çalışmada ise app.py'nin yanında.
app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"))

# İşleri bellekte tutuyoruz (uygulama kapanınca sıfırlanır — sorun değil,
# indirilen dosyalar diskte kalıyor ve archive.txt tekrar indirmeyi engelliyor).
jobs = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)


# --- Yardımcılar ----------------------------------------------------------

def _update(job_id, **fields):
    """Bir işin durumunu güvenli şekilde günceller."""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _make_progress_hook(job_id):
    """yt-dlp'nin indirme sırasında çağıracağı ilerleme fonksiyonu."""
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0
            _update(
                job_id,
                status="downloading",
                percent=round(percent, 1),
                speed=d.get("_speed_str", "").strip(),
                eta=d.get("_eta_str", "").strip(),
                title=(d.get("info_dict") or {}).get("title") or jobs.get(job_id, {}).get("title"),
            )
        elif status == "finished":
            # İndirme bitti; şimdi ffmpeg dönüştürme/birleştirme aşaması olabilir.
            _update(job_id, status="processing", percent=100)
    return hook


def _ydl_opts(job_id, fmt, quality):
    """Ses / video için yt-dlp seçeneklerini hazırlar."""
    outtmpl = os.path.join(DOWNLOAD_DIR, FILENAME_TEMPLATE)
    archive = ARCHIVE_AUDIO if fmt == "audio" else ARCHIVE_VIDEO
    common = {
        "outtmpl": outtmpl,
        "download_archive": archive,        # aynı şeyi iki kez indirmeyi engeller
        "progress_hooks": [_make_progress_hook(job_id)],
        "ignoreerrors": False,
        "noplaylist": False,                # playlist linki verilirse hepsini indir
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,           # dosya adlarını güvenli tut
        "restrictfilenames": False,
        # Takılan bir bağlantı bir işçiyi (worker) sonsuza dek kilitlemesin:
        "socket_timeout": 30,               # 30 sn cevap gelmezse hata ver
        "retries": 5,
        "fragment_retries": 5,
    }
    if FFMPEG:
        common["ffmpeg_location"] = FFMPEG

    if fmt == "audio":
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality or "192",
            }],
        })
    else:  # video
        # En iyi mp4 video + m4a ses, mp4 olarak birleştir.
        common.update({
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
        })
    return common


def _run_download(job_id, url, fmt, quality):
    """Tek bir linki indirir (arka planda thread içinde çalışır)."""
    _update(job_id, status="downloading")
    try:
        opts = _ydl_opts(job_id, fmt, quality)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # info None ise: video arşivde kayıtlı, yt-dlp tekrar indirmeyi atladı.
        if info is None:
            existing = jobs.get(job_id, {}).get("title")
            nice = existing if existing and existing != url else "✓ Zaten indirilmiş"
            _update(job_id, status="done", percent=100, title=nice)
            return

        title = None
        if isinstance(info, dict):
            title = info.get("title")
            if info.get("_type") == "playlist":
                base = info.get("title") or "Playlist"
                # entries = BU çalıştırmada yeni inen; playlist_count = gerçek toplam.
                new_n = len(info.get("entries") or [])
                total = info.get("playlist_count") or new_n
                if new_n == 0:
                    title = f"{base} — hepsi zaten indirilmiş ({total} parça)"
                elif new_n < total:
                    title = f"{base} ({new_n} yeni / {total} toplam)"
                else:
                    title = f"{base} ({total} parça)"

        _update(job_id, status="done", percent=100, title=title or url)
    except yt_dlp.utils.DownloadError as e:
        _update(job_id, status="error", error=_clean_error(str(e)))
    except Exception as e:  # noqa: BLE001 — kullanıcıya anlamlı mesaj göster
        _update(job_id, status="error", error=str(e))


def _clean_error(msg):
    """yt-dlp hata mesajındaki ANSI renk kodlarını ve ERROR: önekini temizler."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    msg = re.sub(r"^\s*ERROR:\s*", "", msg)
    return msg.strip()


def _looks_like_url(s):
    return bool(re.match(r"^https?://", s.strip(), re.IGNORECASE))


def _prune_jobs():
    """Bellek ve /api/jobs cevabı şişmesin diye en eski BİTMİŞ/HATALI işleri düşür.
    Sadece 'done'/'error' işler silinir; sıradaki/inen işlere dokunulmaz.
    Diskteki dosyalar ve arşiv etkilenmez."""
    with jobs_lock:
        if len(jobs) <= MAX_JOBS:
            return
        excess = len(jobs) - MAX_JOBS
        # dict ekleme sırasını korur → en eskiden başla.
        for jid in list(jobs.keys()):
            if excess <= 0:
                break
            if jobs[jid]["status"] in ("done", "error"):
                del jobs[jid]
                excess -= 1


# --- Rotalar --------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("urls", "")
    fmt = data.get("format", "audio")
    quality = data.get("quality", "192")

    if fmt not in ("audio", "video"):
        fmt = "audio"

    # Metni satırlara böl, sadece http(s) linklerini al, tekrarları ayıkla.
    urls = []
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if line and _looks_like_url(line) and line not in seen:
            seen.add(line)
            urls.append(line)

    if not urls:
        return jsonify({"error": "Geçerli link bulunamadı (http/https ile başlamalı)."}), 400

    new_ids = []
    for url in urls:
        job_id = uuid.uuid4().hex[:8]
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "url": url,
                "format": fmt,
                "status": "queued",
                "title": url,
                "percent": 0,
                "speed": "",
                "eta": "",
                "error": None,
            }
        new_ids.append(job_id)
        executor.submit(_run_download, job_id, url, fmt, quality)

    _prune_jobs()
    return jsonify({"queued": new_ids, "count": len(new_ids)})


@app.route("/api/jobs")
def api_jobs():
    with jobs_lock:
        # En yeni en üstte olacak şekilde ters çevir.
        return jsonify(list(reversed(list(jobs.values()))))


@app.route("/api/clear", methods=["POST"])
def api_clear():
    """Bitmiş/hatalı işleri listeden temizler (dosyalara dokunmaz)."""
    with jobs_lock:
        for jid in list(jobs.keys()):
            if jobs[jid]["status"] in ("done", "error"):
                del jobs[jid]
    return jsonify({"ok": True})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """İndirilenler klasörünü sistem dosya yöneticisinde açar (Mac/Windows/Linux)."""
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.Popen(["open", DOWNLOAD_DIR])
        elif os.name == "nt":
            os.startfile(DOWNLOAD_DIR)  # noqa: only exists on Windows
        else:
            subprocess.Popen(["xdg-open", DOWNLOAD_DIR])
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Sunucuyu kapatır. Arayüzdeki '⏻ Kapat' butonu buraya istek atar; böylece
    teknik olmayan kullanıcı terminal bilmeden sunucuyu durdurabilir.
    Sadece 127.0.0.1'den erişilebildiği için dışarıdan tetiklenemez.
    Önce cevabı gönderip kısa bir gecikmeyle süreci sonlandırıyoruz."""
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Windows .exe (--noconsole) modunda stdout/stderr None olur; print/werkzeug
    # log'u çökmesin diye .exe'nin yanına bir log dosyasına yönlendir. Bu log
    # aynı zamanda bir sorun olursa teşhis için işe yarar.
    if FROZEN and (sys.stdout is None or sys.stderr is None):
        _logf = open(os.path.join(BASE_DIR, "muzik-indirici.log"), "a",
                     encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf

    URL = "http://127.0.0.1:5001"
    print("\n  🎵  Music Downloader çalışıyor")
    print(f"  →  Tarayıcıda aç:  {URL}\n")

    # Windows .exe olarak çalışırken tarayıcıyı açacak ayrı bir launcher yok;
    # o yüzden sunucu kalkar kalkmaz tarayıcıyı uygulamanın kendisi açar.
    # (Mac'te tarayıcıyı .app/run.sh açtığı için orada tekrar açmıyoruz.)
    if FROZEN:
        threading.Timer(1.5, lambda: webbrowser.open(URL)).start()

    # host='127.0.0.1' KRİTİK: uygulama sadece kendi bilgisayarından erişilebilir,
    # ağdaki başka cihazlar bağlanamaz. Güvenlik için böyle bırak.
    app.run(host="127.0.0.1", port=5001, threaded=True, debug=False)
