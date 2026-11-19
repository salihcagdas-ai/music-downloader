"""A loopback-only YouTube/Facebook downloader with bounded background jobs.

Only download media you own or have permission to save. Downloads and format
archives remain local. No browser cookies or account credentials are loaded.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yt_dlp
from flask import Flask, jsonify, render_template, request

FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
MAX_CONCURRENT = 3
MAX_JOBS = 200
MAX_URLS_PER_REQUEST = 20
MAX_PLAYLIST_ITEMS = 50
TERMINAL = frozenset({"done", "error", "skipped"})
QUALITY = frozenset({"128", "192", "256", "320"})
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
FACEBOOK_HOSTS = frozenset(
    {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "mbasic.facebook.com",
        "fb.watch",
        "www.fb.watch",
    }
)
FILENAME_TEMPLATE = "%(title).160B [%(extractor_key)s-%(id)s].%(ext)s"


def find_ffmpeg():
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = (
        RESOURCE_DIR / executable,
        BASE_DIR / executable,
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    )
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def find_js_runtimes():
    for runtime in ("deno", "node"):
        location = shutil.which(runtime)
        if location:
            return {runtime: {"path": location}}
    return {}


def normalize_url(value):
    """Accept supported public-site URL shapes without DNS or network requests."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Each URL must be text of at most 2,048 characters")
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("URLs cannot contain whitespace, control characters or backslashes")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ValueError("Invalid URL") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only public HTTP(S) YouTube or Facebook links are supported")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not allowed")
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        raise ValueError("Only standard HTTP(S) ports are supported")
    host = parts.hostname.lower()
    if host not in YOUTUBE_HOSTS | FACEBOOK_HOSTS:
        raise ValueError("Only YouTube and Facebook video or playlist links are supported")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = dict(pairs)
    if any(
        key.lower() in {"u", "url", "redirect", "redirect_url", "redirect_uri", "next"}
        for key, _ in pairs
    ):
        raise ValueError("Redirect URLs are not supported")
    if host in YOUTUBE_HOSTS:
        if parts.path == "/playlist":
            identifier = query.get("list", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", identifier):
                raise ValueError("A valid YouTube playlist identifier is required")
            return "https://www.youtube.com/playlist?" + urlencode({"list": identifier})
        if host == "youtu.be":
            identifier = parts.path.strip("/")
        elif parts.path == "/watch":
            identifier = query.get("v", "")
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", parts.path)
            identifier = match.group(1) if match else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
            raise ValueError("Use a YouTube video, Shorts, live-video or playlist link")
        # A watch URL with list= still means one video; use /playlist for a playlist.
        return "https://www.youtube.com/watch?" + urlencode({"v": identifier})
    if host in {"fb.watch", "www.fb.watch"}:
        if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parts.path):
            raise ValueError("Invalid Facebook short link")
        return "https://fb.watch/" + parts.path.strip("/") + "/"
    path = parts.path.rstrip("/")
    is_video = (path == "/watch" and query.get("v", "").isdigit()) or bool(
        re.fullmatch(r"/reel/[0-9]+", path)
        or re.fullmatch(r"/[^/]+/videos/(?:[^/]+/)?[0-9]+", path)
        or re.fullmatch(r"/share/[rv]/[A-Za-z0-9_-]+", path)
        or (path in {"/video.php", "/watch.php"} and query.get("v", "").isdigit())
    )
    if not is_video:
        raise ValueError("Use a Facebook video, reel, share-video or fb.watch link")
    kept = [
        (key, val)
        for key, val in pairs
        if not key.lower().startswith("utm_") and key not in {"fbclid", "mibextid"}
    ]
    return urlunsplit(
        ("https", "www.facebook.com", path + ("/" if path == "/watch" else ""), urlencode(kept), "")
    )


def clean_error(message):
    message = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(message))
    message = re.sub(r"^\s*ERROR:\s*", "", message)
    return " ".join(message.split())[:1000]


class ArchiveAwareYoutubeDL(yt_dlp.YoutubeDL):
    """Observe actual archive matches; None alone is not proof of a skip."""

    def __init__(self, options):
        self.skipped_ids = set()
        super().__init__(options)

    def in_download_archive(self, info):
        skipped = super().in_download_archive(info)
        if skipped:
            extractor = str(info.get("extractor_key") or info.get("ie_key") or "").lower()
            self.skipped_ids.add((extractor, info.get("id")))
        return skipped

    @property
    def skipped_count(self):
        return len(self.skipped_ids)


class JobManager:
    def __init__(self, base_dir, ffmpeg, js_runtimes, downloader_factory, executor):
        self.base_dir = Path(base_dir)
        self.download_dir = self.base_dir / "downloads"
        self.ffmpeg = ffmpeg
        self.js_runtimes = js_runtimes
        self.downloader_factory = downloader_factory
        self.executor = executor
        self.jobs = {}
        self.lock = threading.Lock()
        # Serialize each format so concurrent playlists cannot race one archive
        # or overwrite the same media file. Audio and video can run together.
        self.format_locks = {"audio": threading.Lock(), "video": threading.Lock()}
        self.closing = False

    def update(self, identifier, **fields):
        with self.lock:
            row = self.jobs.get(identifier)
            if row is not None and row["status"] not in TERMINAL:
                row.update(fields)

    def progress_hook(self, identifier):
        def progress(data):
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                percent = 100 * downloaded / total if total else 0
                percent = max(0, min(100, percent)) if math.isfinite(percent) else 0
                fields = {
                    "status": "downloading",
                    "percent": round(percent, 1),
                    "speed": clean_error(data.get("_speed_str", "")),
                    "eta": clean_error(data.get("_eta_str", "")),
                }
                title = (data.get("info_dict") or {}).get("title")
                if isinstance(title, str) and title:
                    fields["title"] = title[:500]
                self.update(identifier, **fields)
            elif data.get("status") == "finished":
                self.update(identifier, status="processing", percent=100)

        return progress

    def options(self, identifier, fmt, quality, completed):
        folder = self.download_dir / fmt
        folder.mkdir(parents=True, exist_ok=True)
        expected_extension = ".mp3" if fmt == "audio" else ".mp4"

        def finished(filename):
            path = Path(filename).resolve()
            if (
                folder.resolve() not in path.parents
                or path.suffix.lower() != expected_extension
                or not path.is_file()
            ):
                raise RuntimeError(
                    "Postprocessing did not produce the requested local output format"
                )
            completed.add(str(path))

        options = {
            "outtmpl": str(folder / FILENAME_TEMPLATE),
            "download_archive": str(self.base_dir / f"archive-{fmt}.txt"),
            "progress_hooks": [self.progress_hook(identifier)],
            "post_hooks": [finished],
            "ignoreerrors": False,
            "noplaylist": True,
            "playlistend": MAX_PLAYLIST_ITEMS,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "windowsfilenames": True,
            "restrictfilenames": False,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "ffmpeg_location": self.ffmpeg,
            "cachedir": False,
            "allowed_extractors": ["youtube.*", "facebook.*"],
            "js_runtimes": self.js_runtimes,
            "remote_components": set(),
            "overwrites": False,
            "final_ext": expected_extension.lstrip("."),
            "color": "no_color",
        }
        if fmt == "audio":
            options.update(
                format="bestaudio/best",
                postprocessors=[
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": quality,
                        "nopostoverwrites": True,
                    }
                ],
            )
        else:
            options.update(
                format="bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                merge_output_format="mp4",
                postprocessors=[
                    {
                        "key": "FFmpegVideoRemuxer",
                        "preferedformat": "mp4",
                    }
                ],
            )
        return options

    def run(self, identifier, url, fmt, quality):
        completed = set()
        try:
            with self.format_locks[fmt]:
                self.update(identifier, status="downloading")
                options = self.options(identifier, fmt, quality, completed)
                with self.downloader_factory(options) as downloader:
                    info = downloader.extract_info(url, download=True)
                    skipped = int(getattr(downloader, "skipped_count", 0))
                title = info.get("title") if isinstance(info, dict) else None
                if not isinstance(title, str) or not title:
                    title = "Already in the archive" if skipped else "Download completed"
                message = f"Completed {len(completed)} item(s); skipped {skipped} archived item(s)."
                if isinstance(info, dict) and info.get("_type") in {"playlist", "multi_video"}:
                    message += (
                        f" At most the first {MAX_PLAYLIST_ITEMS} playlist entries are considered."
                    )
                if not completed and not skipped:
                    raise RuntimeError(
                        "No downloadable media or confirmed archive match was returned"
                    )
                self.update(
                    identifier,
                    status="done" if completed else "skipped",
                    percent=100,
                    title=title[:500],
                    message=message,
                    speed="",
                    eta="",
                )
        except Exception as error:
            self.update(
                identifier,
                status="error",
                error=clean_error(error),
                speed="",
                eta="",
                message=(
                    f"Completed {len(completed)} item(s) before the error; "
                    "completed files remain local."
                ),
            )

    def submit(self, urls, fmt, quality):
        with self.lock:
            if self.closing:
                return {"error": "The application is shutting down"}, 503
            active = {
                (row["url"], row["format"]): row["id"]
                for row in self.jobs.values()
                if row["status"] not in TERMINAL
            }
            duplicate = [
                {"url": url, "id": active[(url, fmt)]} for url in urls if (url, fmt) in active
            ]
            new_urls = [url for url in urls if (url, fmt) not in active]
            if len(active) + len(new_urls) > MAX_JOBS:
                return {
                    "error": "All 200 job slots are active; wait for some downloads to finish"
                }, 429
            excess = max(0, len(self.jobs) + len(new_urls) - MAX_JOBS)
            for identifier in list(self.jobs):
                if not excess:
                    break
                if self.jobs[identifier]["status"] in TERMINAL:
                    del self.jobs[identifier]
                    excess -= 1
            pending = []
            for url in new_urls:
                identifier = uuid.uuid4().hex
                self.jobs[identifier] = {
                    "id": identifier,
                    "url": url,
                    "format": fmt,
                    "status": "queued",
                    "title": url,
                    "percent": 0,
                    "speed": "",
                    "eta": "",
                    "error": None,
                    "message": "Waiting for a worker and format archive",
                }
                pending.append((identifier, url))
        queued = []
        for identifier, url in pending:
            try:
                self.executor.submit(self.run, identifier, url, fmt, quality)
                queued.append(identifier)
            except RuntimeError:
                self.update(identifier, status="error", error="The worker queue is unavailable")
        result = {"queued": queued, "count": len(queued), "duplicates": duplicate}
        if pending and not queued:
            result["error"] = "The worker queue is unavailable"
            return result, 503
        return result, 202 if queued else 200


def open_download_folder(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def create_app(
    config=None,
    *,
    downloader_factory=None,
    executor=None,
    folder_opener=None,
    shutdown_callback=None,
):
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "static"),
    )
    app.config.update(BASE_DIR=BASE_DIR, APP_PORT=5001, MAX_CONTENT_LENGTH=65536)
    if config:
        app.config.update(config)
    if "FFMPEG" not in app.config:
        app.config["FFMPEG"] = find_ffmpeg()
    if "JS_RUNTIMES" not in app.config:
        app.config["JS_RUNTIMES"] = find_js_runtimes()
    manager = JobManager(
        app.config["BASE_DIR"],
        app.config["FFMPEG"],
        app.config["JS_RUNTIMES"],
        downloader_factory or ArchiveAwareYoutubeDL,
        executor if executor is not None else ThreadPoolExecutor(max_workers=MAX_CONCURRENT),
    )
    token = secrets.token_urlsafe(32)
    app.extensions["job_manager"] = manager
    app.extensions["csrf_token"] = token
    opener = folder_opener or open_download_folder
    expected_hosts = {f"127.0.0.1:{app.config['APP_PORT']}", f"localhost:{app.config['APP_PORT']}"}

    @app.before_request
    def protect_local_service():
        if request.host.lower() not in expected_hosts:
            return jsonify(error="Untrusted Host header"), 403
        try:
            if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
                return jsonify(error="Only loopback clients are allowed"), 403
        except ValueError:
            return jsonify(error="Only loopback clients are allowed"), 403
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"http://{request.host.lower()}":
            return jsonify(error="Cross-origin requests are not allowed"), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Cross-site requests are not allowed"), 403
        if request.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("X-CSRF-Token", ""), token
        ):
            return jsonify(error="Missing or invalid request token; reload the page"), 403
        return None

    @app.after_request
    def security_headers(response):
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
            }
        )
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Request body is too large"), 413

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=token)

    @app.post("/api/download")
    def download():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with urls, format and quality"), 400
        raw, fmt, quality = (
            data.get("urls"),
            data.get("format", "audio"),
            data.get("quality", "192"),
        )
        if not isinstance(raw, str) or not isinstance(fmt, str) or fmt not in {"audio", "video"}:
            return jsonify(error="urls must be text and format must be audio or video"), 400
        if not isinstance(quality, str) or quality not in QUALITY:
            return jsonify(error="quality must be 128, 192, 256 or 320"), 400
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines or len(lines) > MAX_URLS_PER_REQUEST:
            return jsonify(
                error=f"Submit between 1 and {MAX_URLS_PER_REQUEST} links per request"
            ), 400
        try:
            urls = list(dict.fromkeys(normalize_url(line) for line in lines))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if not manager.ffmpeg or not Path(manager.ffmpeg).is_file():
            return jsonify(
                error="FFmpeg was not found. Install FFmpeg and ffprobe, then restart the app"
            ), 503
        probe = Path(manager.ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if not probe.is_file():
            return jsonify(
                error="ffprobe is missing beside FFmpeg. Install both tools, then restart the app"
            ), 503
        if any(urlsplit(url).hostname in YOUTUBE_HOSTS for url in urls) and not manager.js_runtimes:
            return jsonify(
                error=(
                    "YouTube needs a supported JavaScript runtime. "
                    "Install Deno or Node.js 22+, then restart the app"
                )
            ), 503
        result, status = manager.submit(urls, fmt, quality)
        return jsonify(result), status

    @app.get("/api/jobs")
    def job_list():
        with manager.lock:
            rows = [dict(row) for row in reversed(list(manager.jobs.values()))]
        return jsonify(rows)

    @app.post("/api/clear")
    def clear():
        with manager.lock:
            terminal_ids = [
                identifier for identifier, row in manager.jobs.items() if row["status"] in TERMINAL
            ]
            for identifier in terminal_ids:
                del manager.jobs[identifier]
        return jsonify(ok=True, cleared=len(terminal_ids))

    @app.post("/api/open-folder")
    def open_folder():
        try:
            opener(manager.download_dir)
        except OSError:
            return jsonify(error="Could not open the downloads folder"), 500
        return jsonify(ok=True)

    @app.post("/api/shutdown")
    def shutdown():
        with manager.lock:
            if any(row["status"] not in TERMINAL for row in manager.jobs.values()):
                return jsonify(error="Wait for active downloads to finish before closing"), 409
            if shutdown_callback is None:
                return jsonify(error="Shutdown is not available in this server mode"), 503
            manager.closing = True
        shutdown_callback()
        return jsonify(ok=True)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser at startup"
    )
    args = parser.parse_args(argv)
    if FROZEN and (sys.stdout is None or sys.stderr is None):
        log_file = open(BASE_DIR / "music-downloader.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file
    from werkzeug.serving import make_server

    def shutdown_server():
        threading.Thread(target=server.shutdown, daemon=True).start()

    application = create_app(shutdown_callback=shutdown_server)
    manager = application.extensions["job_manager"]
    server = make_server("127.0.0.1", 5001, application, threaded=True)
    print("Music Downloader is available at http://127.0.0.1:5001")
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:5001")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.executor.shutdown(wait=True, cancel_futures=True)
        server.server_close()


if __name__ == "__main__":
    main()
