# Third-party components

Music Downloader provides a local interface around existing media tools.

| Component | Purpose | Upstream license/source |
| --- | --- | --- |
| Flask and its dependencies | Local HTTP server and HTML templates | [Flask](https://github.com/pallets/flask) |
| yt-dlp and its default dependencies | Media extraction, download and metadata | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| yt-dlp-ejs | JavaScript challenge solver scripts used by yt-dlp | [yt-dlp/ejs](https://github.com/yt-dlp/ejs) |
| FFmpeg / ffprobe | Audio conversion, inspection and video merging | [FFmpeg legal information](https://ffmpeg.org/legal.html) |
| Deno or Node.js | External JavaScript runtime | [Deno](https://github.com/denoland/deno), [Node.js](https://github.com/nodejs/node) |
| PyInstaller | Optional Windows packaging | [PyInstaller](https://github.com/pyinstaller/pyinstaller) |

Dependencies retain their own licenses. This repository does not include media,
FFmpeg binaries or a JavaScript runtime. Install these tools separately.

The Windows build includes installed Python packages. Its artifact must be kept
with the generated `THIRD_PARTY_NOTICES/` directory and this document. The build
script collects available upstream license files and a package inventory; this
does not replace the individual licenses. The repository's own source currently
has no declared license.
