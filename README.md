# 🎵 Müzik İndirici

YouTube ve Facebook linklerini **ses (mp3)** veya **video (mp4)** olarak indiren, **sadece kendi bilgisayarında** çalışan küçük bir uygulama.

İnternete/ağa açılmaz (yalnızca `127.0.0.1`), hesap-şifre istemez, dosyalar bilgisayarında kalır.

---

## 🚀 Kullanım (en kolay yol)

Teknik bilgi gerektirmez. 3 adım:

### 1. Başlat
Finder'da **`Müzik İndirici`** uygulamasına **çift tıkla.**
Birkaç saniye sonra tarayıcıda indirme ekranı kendiliğinden açılır.

> İlk çift tıklamada macOS "geliştirici doğrulanamadı" derse: uygulamaya **sağ tıkla → Aç** de, bir kez onayla. Sonraki seferler direkt açılır.

### 2. İndir
1. Linkleri kutuya yapıştır — **her satıra bir tane** (YouTube / Facebook).
2. **🎧 Ses (mp3)** ya da **🎬 Video (mp4)** seç.
3. **İndir**'e bas. (Kısayol: `Cmd + Enter`)
4. İlerlemeyi ekrandan izle. Bittiğinde **📂 Klasörü aç** ile dosyaları gör.

Dosyalar `downloads/` klasörüne iner.

### 3. Kapat
İşin bitince ekranın sağ üstündeki **⏻ Kapat** butonuna bas.
"Sunucu kapatıldı" yazısını görünce sekmeyi kapatabilirsin. Hepsi bu — terminal, komut, hiçbir şey gerekmez.

Tekrar indirmek istediğinde yine **Müzik İndirici**'ye çift tıkla.

---

## ℹ️ Bilmen gereken küçük şeyler

- **Aynı şey iki kez inmez.** İndirilenler `archive-audio.txt` / `archive-video.txt` dosyalarında takip edilir. Yarıda kalırsa uygulamayı tekrar açıp aynı listeyi yapıştırman yeter — inenleri atlar, sadece eksikleri tamamlar. Bir şeyi bilerek tekrar indirmek istersen ilgili arşiv dosyasından o satırı sil.
- **Ses ve video ayrı sayılır** — bir videoyu hem mp3 hem mp4 indirebilirsin.
- **Playlist linki** verirsen içindeki tüm parçaları indirir.
- **Kırmızı "hata"** çıkan link olursa (video kaldırılmış, erişime kapalı vb.) gerisini etkilemez; çoğu zaman tekrar denemek yeterlidir.
- Aynı anda **3 indirme** yapar.

---

## 🌙 Gece boyu / çok sayıda indirme

Çok link varsa ve bilgisayarın gece uyumasını istemiyorsan, Terminal'den:

```bash
cd "/path/to/music-downloader"
caffeinate -i ./run.sh
```

`caffeinate -i` indirme bitene kadar Mac'in uykuya geçmesini engeller. Bittiğinde Terminal'de `Ctrl+C` ya da ekrandan **⏻ Kapat**.

---

## 🪟 Windows sürümü (tek .exe, kurulum gerektirmez)

Windows için **Python/ffmpeg kurmaya gerek olmadan** çalışan tek bir `.exe` üretilir —
GitHub'ın Windows sunucusunda otomatik derlenir.

**İndirmek için:**
1. GitHub'da depoya git → üstteki **Actions** sekmesi.
2. **"Windows EXE derle"** iş akışının en son başarılı (yeşil ✓) çalışmasına tıkla.
3. Alttaki **Artifacts** kısmından **`MuzikIndirici-Windows`**'u indir, zip'i aç.
4. Çıkan **`MuzikIndirici.exe`**'yi Windows'ta çift tıkla — tarayıcı açılır, gerisi Mac'le aynı (yapıştır → indir → ⏻ Kapat).

> Elle yeni derleme: Actions → "Windows EXE derle" → **Run workflow**.
> İndirilenler `.exe`'nin bulunduğu klasördeki `downloads/` altına iner.

---

## 🔧 Nasıl çalışıyor (teknik özet)

| Parça | Görevi |
|---|---|
| **`Müzik İndirici.app`** | Çift tıkla başlatıcı: sunucuyu açar, tarayıcıyı açar. |
| **`app.py`** | Flask sunucusu — arayüzü sunar, indirmeleri [yt-dlp](https://github.com/yt-dlp/yt-dlp) ile yönetir. Sadece `127.0.0.1:5001`'e bağlanır. |
| **`templates/index.html`** | Arayüz — link yapıştır, ilerleme çubukları, ⏻ Kapat butonu. |
| **`run.sh`** | Terminal'den başlatıcı (gece indirmesi / ileri kullanım). |
| **ffmpeg** | mp3'e çevirme ve mp4 birleştirme için. |

- İndirme isteği → `/api/download`; durum → saniyede bir `/api/jobs`; kapatma → `/api/shutdown`.
- **⏻ Kapat**, sunucu sürecini tamamen sonlandırır (`os._exit`). Uygulama Finder'dan açıldığında görünür bir pencere olmadığı için kapatmanın ana yolu budur.
- Uygulama `127.0.0.1`'e bağlandığından ağdaki başka cihazlar erişemez — bu bir güvenlik tercihidir, böyle bırakılmalı.

---

## Gereksinimler (tek seferlik kurulum)

Bu bilgisayarda zaten kurulu. Sıfırdan bir Mac'e taşırsan:

- **Python 3.9+** — `python.org`'dan ya da `xcode-select --install`
- **ffmpeg** — `brew install ffmpeg`

İlk çalıştırmada uygulama gerekli Python paketlerini (`Flask`, `yt-dlp`) kendi sanal ortamına (`.venv`) otomatik kurar.
