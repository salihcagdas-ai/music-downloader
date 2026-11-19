"""Offline API and worker regressions; no downloader, browser or OS process runs."""

import socket
from pathlib import Path

import pytest
import yt_dlp

import app as service

BASE_URL = "http://127.0.0.1:5001"


class DeferredExecutor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def submit(self, function, *args):
        self.calls.append((function, args))

    def run_next(self):
        function, args = self.calls.pop(0)
        function(*args)

    def shutdown(self, **_kwargs):
        self.closed = True


class ScriptedDownloader:
    def __init__(self, options, scenario="done"):
        self.options = options
        self.scenario = scenario
        self.skipped_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def extract_info(self, _url, *, download):
        assert download
        if self.scenario == "skipped":
            self.skipped_count = 1
            return None
        if self.scenario == "empty":
            return None
        self.options["progress_hooks"][0](
            {
                "status": "downloading",
                "total_bytes": 100,
                "downloaded_bytes": 42,
                "info_dict": {"title": "Synthetic title"},
            }
        )
        self.options["progress_hooks"][0]({"status": "finished"})
        extension = ".mp3" if "preferredcodec" in self.options["postprocessors"][0] else ".mp4"
        if self.scenario == "wrong_format":
            extension = ".webm"
        output = Path(self.options["outtmpl"]).parent / ("synthetic" + extension)
        output.write_bytes(b"synthetic placeholder; not real downloaded media")
        self.options["post_hooks"][0](str(output))
        if self.scenario == "partial_error":
            raise yt_dlp.utils.DownloadError("\x1b[31mERROR: synthetic failure\x1b[0m")
        if self.scenario == "mixed":
            self.skipped_count = 2
            return {
                "_type": "playlist",
                "title": "Synthetic playlist",
                "playlist_count": 100,
                "entries": [None] * 20,
            }
        return {"title": "Synthetic title"}


@pytest.fixture(autouse=True)
def prohibit_external_side_effects(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("An offline test attempted a network, browser or process operation")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(service.subprocess, "Popen", blocked)
    monkeypatch.setattr(service.webbrowser, "open", blocked)
    monkeypatch.setattr(service.os, "_exit", blocked)
    if hasattr(service.os, "startfile"):
        monkeypatch.setattr(service.os, "startfile", blocked)


@pytest.fixture
def app_factory(tmp_path):
    def build(scenario="done", **changes):
        ffmpeg = tmp_path / ("ffmpeg.exe" if service.os.name == "nt" else "ffmpeg")
        ffprobe = tmp_path / ("ffprobe.exe" if service.os.name == "nt" else "ffprobe")
        ffmpeg.touch()
        ffprobe.touch()
        executor = DeferredExecutor()
        opened, stopped = [], []
        config = {
            "TESTING": True,
            "BASE_DIR": tmp_path,
            "FFMPEG": str(ffmpeg),
            "JS_RUNTIMES": {"node": {"path": "synthetic-runtime"}},
        }
        config.update(changes)
        application = service.create_app(
            config,
            executor=executor,
            downloader_factory=lambda opts: ScriptedDownloader(opts, scenario),
            folder_opener=lambda path: opened.append(path),
            shutdown_callback=lambda: stopped.append(True),
        )
        return application, executor, opened, stopped

    return build


def api(application, path, *, method="POST", json=None, headers=None, **kwargs):
    merged = {"X-CSRF-Token": application.extensions["csrf_token"]}
    merged.update(headers or {})
    return application.test_client().open(
        path, method=method, json=json, headers=merged, base_url=BASE_URL, **kwargs
    )


def payload(index=1, fmt="audio"):
    return {
        "urls": f"https://www.youtube.com/watch?v={index:011d}",
        "format": fmt,
        "quality": "192",
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "text",
        4,
        {},
        {"urls": []},
        {"urls": 123},
        {"urls": "https://youtu.be/00000000001", "format": []},
        {"urls": "https://youtu.be/00000000001", "format": "bad"},
        {"urls": "https://youtu.be/00000000001", "quality": 192},
        {"urls": "https://youtu.be/00000000001", "quality": "999"},
    ],
)
def test_invalid_payload_does_not_create_jobs(app_factory, value):
    application, executor, *_ = app_factory()
    assert api(application, "/api/download", json=value).status_code == 400
    assert not executor.calls
    assert application.extensions["job_manager"].jobs == {}


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/file",
        "http://localhost/private",
        "http://127.0.0.1/a",
        "http://[::1]/a",
        "http://192.168.1.2/a",
        "https://youtube.com.evil.invalid/watch?v=00000000001",
        "https://user:pass@youtube.com/watch?v=00000000001",
        "https://@youtube.com/watch?v=00000000001",
        "https://youtube.com:1234/watch?v=00000000001",
        "https://youtube.com/redirect?url=http://localhost/",
        "https://facebook.com/l.php?u=http://localhost/",
        "https://youtu.be/short",
        "https://youtube.com/watch?v=00000000001\\evil",
        "https://youtube.com/watch?v=00000000001 x",
        "https://facebook.com/",
        "https://not-youtube.com/watch?v=00000000001",
    ],
)
def test_unsupported_or_private_url_is_rejected(url):
    with pytest.raises(ValueError):
        service.normalize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/00000000001?si=tracking",
        "http://m.youtube.com/watch?v=00000000001&list=PL0000000000",
        "https://www.youtube.com/shorts/00000000001",
    ],
)
def test_equivalent_youtube_urls_have_one_canonical_identity(url):
    assert service.normalize_url(url) == "https://www.youtube.com/watch?v=00000000001"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/watch/?v=123456",
        "https://m.facebook.com/reel/123456/",
        "https://www.facebook.com/example/videos/123456/",
        "https://fb.watch/abc123/",
        "https://www.facebook.com/share/v/abc123/",
        "https://www.youtube.com/playlist?list=PL0000000000",
    ],
)
def test_supported_public_link_shapes(url):
    assert service.normalize_url(url).startswith("https://")


def test_invalid_url_rejects_the_entire_batch(app_factory):
    application, executor, *_ = app_factory()
    value = payload()
    value["urls"] += "\nhttp://localhost/secret"
    assert api(application, "/api/download", json=value).status_code == 400
    assert not executor.calls


def test_per_request_limit_and_request_body_limit(app_factory):
    application, executor, *_ = app_factory()
    value = payload()
    value["urls"] = "\n".join(f"https://youtu.be/{index:011d}" for index in range(21))
    assert api(application, "/api/download", json=value).status_code == 400
    assert api(application, "/api/download", json={"urls": "x" * 70000}).status_code == 413
    assert not executor.calls


def test_all_api_routes_require_token_even_get_and_shutdown(app_factory):
    application, _, opened, stopped = app_factory()
    for path, method in [
        ("/api/jobs", "GET"),
        ("/api/download", "POST"),
        ("/api/clear", "POST"),
        ("/api/open-folder", "POST"),
        ("/api/shutdown", "POST"),
    ]:
        result = application.test_client().open(path, method=method, base_url=BASE_URL)
        assert result.status_code == 403
    assert opened == [] and stopped == []
    assert api(application, "/api/jobs", method="GET").status_code == 200


def test_host_origin_and_nonloopback_clients_are_rejected(app_factory):
    application, *_ = app_factory()
    for headers in [
        {"Host": "evil.invalid:5001"},
        {"Origin": "https://evil.invalid"},
        {"Origin": "null"},
        {"Origin": "http://localhost:5001"},
        {"Sec-Fetch-Site": "cross-site"},
    ]:
        assert api(application, "/api/jobs", method="GET", headers=headers).status_code == 403
    assert (
        api(
            application, "/api/jobs", method="GET", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}
        ).status_code
        == 403
    )
    assert (
        api(application, "/api/jobs", method="GET", headers={"Origin": BASE_URL}).status_code == 200
    )
    assert (
        application.test_client().get("/", base_url="http://evil.invalid:5001").status_code == 403
    )


def test_tokens_are_distinct_for_each_application_instance(app_factory):
    first, *_ = app_factory()
    second, *_ = app_factory()
    assert first.extensions["csrf_token"] != second.extensions["csrf_token"]
    assert (
        api(
            first,
            "/api/jobs",
            method="GET",
            headers={"X-CSRF-Token": second.extensions["csrf_token"]},
        ).status_code
        == 403
    )


def test_duplicate_active_urls_are_deduplicated_per_format(app_factory):
    application, executor, *_ = app_factory()
    first = api(application, "/api/download", json=payload()).get_json()
    second = payload()
    second["urls"] = "https://youtu.be/00000000001"
    repeated = api(application, "/api/download", json=second)
    assert repeated.status_code == 200
    assert repeated.get_json()["count"] == 0
    assert repeated.get_json()["duplicates"][0]["id"] == first["queued"][0]
    assert api(application, "/api/download", json=payload(fmt="video")).status_code == 202
    assert len(executor.calls) == 2


def test_global_cap_never_drops_active_jobs_and_prunes_only_terminal(app_factory):
    application, executor, *_ = app_factory()
    for batch in range(10):
        value = payload()
        value["urls"] = "\n".join(
            f"https://youtu.be/{batch * 20 + index:011d}" for index in range(20)
        )
        assert api(application, "/api/download", json=value).status_code == 202
    manager = application.extensions["job_manager"]
    original = list(manager.jobs)
    assert len(original) == 200 and len(executor.calls) == 200
    assert api(application, "/api/download", json=payload(999)).status_code == 429
    assert list(manager.jobs) == original
    assert api(application, "/api/clear").get_json()["cleared"] == 0
    for identifier in original[:3]:
        manager.update(identifier, status="done")
    assert api(application, "/api/download", json=payload(999)).status_code == 202
    assert len(manager.jobs) == 200 and original[0] not in manager.jobs
    assert all(identifier in manager.jobs for identifier in original[3:])


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("done", "done"),
        ("skipped", "skipped"),
        ("empty", "error"),
        ("mixed", "done"),
        ("partial_error", "error"),
        ("wrong_format", "error"),
    ],
)
def test_worker_outcome_uses_completed_files_and_confirmed_archive_matches(
    app_factory, scenario, expected
):
    application, executor, *_ = app_factory(scenario)
    response = api(application, "/api/download", json=payload())
    assert response.status_code == 202
    identifier = response.get_json()["queued"][0]
    executor.run_next()
    row = api(application, "/api/jobs", method="GET").get_json()[0]
    assert row["id"] == identifier and row["status"] == expected
    if scenario == "mixed":
        assert "Completed 1 item(s); skipped 2" in row["message"]
        assert "first 50" in row["message"]
    if scenario == "partial_error":
        assert row["error"] == "synthetic failure"
        assert "Completed 1" in row["message"]
    if scenario == "skipped":
        assert row["title"] == "Already in the archive"
    manager = application.extensions["job_manager"]
    manager.progress_hook(identifier)(
        {"status": "downloading", "total_bytes": 1, "downloaded_bytes": 1}
    )
    assert manager.jobs[identifier]["status"] == expected


def test_video_options_remux_the_fallback_and_keep_archives_separate(app_factory):
    application, *_ = app_factory()
    manager = application.extensions["job_manager"]
    audio = manager.options("test", "audio", "192", set())
    video = manager.options("test", "video", "192", set())
    assert audio["download_archive"] != video["download_archive"]
    assert video["merge_output_format"] == "mp4"
    assert video["postprocessors"] == [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    assert video["playlistend"] == 50
    assert "cookiesfrombrowser" not in video and "cookiefile" not in video
    assert video["remote_components"] == set()


def test_archive_observer_counts_a_real_local_archive_match_once(tmp_path):
    archive = tmp_path / "archive.txt"
    archive.write_text("youtube 00000000001\n", encoding="utf-8")
    with service.ArchiveAwareYoutubeDL(
        {"download_archive": str(archive), "quiet": True, "cachedir": False}
    ) as downloader:
        assert downloader.in_download_archive({"ie_key": "Youtube", "id": "00000000001"})
        assert downloader.in_download_archive({"ie_key": "Youtube", "id": "00000000001"})
        assert downloader.skipped_count == 1
        assert not downloader.in_download_archive({"ie_key": "Youtube", "id": "00000000002"})


def test_missing_ffmpeg_fails_before_queueing(app_factory):
    application, executor, *_ = app_factory(FFMPEG=None)
    result = api(application, "/api/download", json=payload())
    assert result.status_code == 503 and "FFmpeg" in result.get_json()["error"]
    assert not executor.calls


def test_missing_ffprobe_fails_before_queueing(app_factory, tmp_path):
    alternate = tmp_path / "separate-tools"
    alternate.mkdir()
    ffmpeg = alternate / ("ffmpeg.exe" if service.os.name == "nt" else "ffmpeg")
    ffmpeg.touch()
    application, executor, *_ = app_factory(FFMPEG=str(ffmpeg))
    result = api(application, "/api/download", json=payload())
    assert result.status_code == 503 and "ffprobe" in result.get_json()["error"]
    assert not executor.calls


@pytest.mark.parametrize("fmt", ["audio", "video"])
def test_pinned_ytdlp_accepts_real_options_without_network_or_subprocess(app_factory, fmt):
    application, *_ = app_factory()
    manager = application.extensions["job_manager"]
    options = manager.options("test", fmt, "192", set())
    with service.ArchiveAwareYoutubeDL(options) as downloader:
        assert "Youtube" in downloader._ies and "Facebook" in downloader._ies
        assert "Generic" not in downloader._ies


@pytest.mark.parametrize("fmt,extension", [("audio", ".mp3"), ("video", ".mp4")])
def test_existing_final_media_is_preserved_when_archive_is_missing(
    app_factory, monkeypatch, fmt, extension
):
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP

    # The placeholder is not encoded media. Stub only codec probing, retaining
    # yt-dlp's real existing-file check and postprocessing decision logic.
    monkeypatch.setattr(FFmpegExtractAudioPP, "get_audio_codec", lambda *_: "mp3")
    application, *_ = app_factory()
    manager = application.extensions["job_manager"]
    completed = set()
    options = manager.options("test", fmt, "192", completed)
    info = {
        "id": "00000000001",
        "title": "Synthetic example",
        "ext": "webm",
        "url": "https://example.invalid/synthetic.webm",
        "extractor_key": "Youtube",
        "extractor": "youtube",
        "format_id": "test",
        "format": "test",
        "acodec": "opus",
        "vcodec": "none" if fmt == "audio" else "vp9",
    }
    with service.ArchiveAwareYoutubeDL(options) as downloader:
        final_file = Path(downloader.prepare_filename(info)).with_suffix(extension)
        original = b"pre-existing user media; must not change"
        final_file.write_bytes(original)

        def forbidden_download(*_args, **_kwargs):
            pytest.fail("Existing final media must prevent another download")

        monkeypatch.setattr(downloader, "dl", forbidden_download)
        downloader.process_info(info)
        assert final_file.read_bytes() == original
        assert completed == {str(final_file.resolve())}
    assert not Path(options["download_archive"]).exists()


def test_audio_postprocessor_preserves_an_existing_target_after_partial_restart(
    app_factory, monkeypatch
):
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP

    application, *_ = app_factory()
    manager = application.extensions["job_manager"]
    options = manager.options("test", "audio", "192", set())
    source = manager.download_dir / "audio" / "partial.webm"
    target = source.with_suffix(".mp3")
    source.write_bytes(b"partial restart input")
    target.write_bytes(b"existing final media")
    monkeypatch.setattr(FFmpegExtractAudioPP, "get_audio_codec", lambda *_: "opus")

    def forbidden_conversion(*_args, **_kwargs):
        pytest.fail("Audio postprocessing must not overwrite the existing final target")

    monkeypatch.setattr(FFmpegExtractAudioPP, "run_ffmpeg", forbidden_conversion)
    with service.ArchiveAwareYoutubeDL(options) as downloader:
        postprocessor = downloader._pps["post_process"][0]
        postprocessor.run({"filepath": str(source), "ext": "webm", "vcodec": "none"})
    assert target.read_bytes() == b"existing final media"
    assert source.read_bytes() == b"partial restart input"


def test_missing_js_runtime_fails_before_youtube_queueing(app_factory):
    application, executor, *_ = app_factory(JS_RUNTIMES={})
    result = api(application, "/api/download", json=payload())
    assert result.status_code == 503 and "JavaScript" in result.get_json()["error"]
    assert not executor.calls


def test_clear_keeps_active_jobs_and_downloaded_files(app_factory, tmp_path):
    application, executor, *_ = app_factory()
    first = api(application, "/api/download", json=payload()).get_json()["queued"][0]
    executor.run_next()
    second = api(application, "/api/download", json=payload(2)).get_json()["queued"][0]
    output = tmp_path / "downloads" / "audio" / "synthetic.mp3"
    assert output.is_file()
    assert api(application, "/api/clear").get_json()["cleared"] == 1
    manager = application.extensions["job_manager"]
    assert first not in manager.jobs and second in manager.jobs
    assert output.is_file()


def test_open_folder_and_shutdown_use_injected_callbacks(app_factory):
    application, executor, opened, stopped = app_factory()
    assert api(application, "/api/open-folder").get_json()["ok"]
    assert opened == [application.extensions["job_manager"].download_dir]
    api(application, "/api/download", json=payload())
    assert api(application, "/api/shutdown").status_code == 409
    assert stopped == []
    executor.run_next()
    assert api(application, "/api/shutdown").get_json()["ok"]
    assert stopped == [True]
    assert api(application, "/api/download", json=payload(2)).status_code == 503


def test_main_headless_binds_loopback_and_closes_resources(app_factory, monkeypatch):
    application, executor, *_ = app_factory()
    calls = []

    class FakeServer:
        def serve_forever(self):
            calls.append("serve")

        def server_close(self):
            calls.append("close")

    def make_server(host, port, app, **kwargs):
        assert host == "127.0.0.1" and port == 5001 and app is application
        assert kwargs == {"threaded": True}
        return FakeServer()

    import werkzeug.serving

    monkeypatch.setattr(werkzeug.serving, "make_server", make_server)
    monkeypatch.setattr(service, "create_app", lambda **_kwargs: application)
    service.main(["--no-browser"])
    assert calls == ["serve", "close"] and executor.closed
