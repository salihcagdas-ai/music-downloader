#!/usr/bin/env bash
#
# Music Downloader'ı başlatır.
# İlk çalıştırmada sanal ortamı (venv) kurar ve bağımlılıkları yükler.
# Sonraki çalıştırmalarda direkt açar.
#
set -e
cd "$(dirname "$0")"

# ffmpeg var mı? (mp3'e çevirmek ve mp4 birleştirmek için gerekli)
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠️  ffmpeg bulunamadı. Kur:  brew install ffmpeg"
  exit 1
fi

# Sanal ortam yoksa kur
if [ ! -d ".venv" ]; then
  echo "📦  İlk kurulum yapılıyor (sanal ortam + bağımlılıklar)…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "✅  Kurulum tamam."
fi

# yt-dlp'yi her açılışta en güncele çek (YouTube sık değişir, bu önemli)
./.venv/bin/pip install --quiet --upgrade yt-dlp >/dev/null 2>&1 || true

# Uygulamayı başlat ve tarayıcıyı aç
( sleep 1.5 && open "http://127.0.0.1:5001" ) &
exec ./.venv/bin/python app.py
