# Binance Futures Analysis Platform V8.0

USDT-M Binance Futures piyasasını tarayıp yüksek güvenilirlikli sinyalleri
Telegram'a gönderen, **sinyal-only** (gerçek emir açmaz, hiçbir zaman) bir
analiz platformu. Tüm 22 modül tamamlandı — 565 test, hepsi geçiyor.

⚠️ **Bu bot gerçek emir vermez.** Sadece Binance'in herkese açık (public)
piyasa verisini okur ve Telegram'a sinyal mesajı gönderir. Binance API
key/secret'a ihtiyacı yoktur.

Bot çalışırken Telegram'dan `/status` (anlık sağlık durumu), `/rapor`
(bugünün özeti), `/scan` (en son tarama sonucu) ve `/help` yazıp anında
cevap alabilirsiniz — sadece `TELEGRAM_CHAT_ID`'de tanımlı kişiye cevap
verir.

---

## 1. Gerekenler

- Bir **Telegram bot token**'ı ve **chat ID**'niz.
- Railway hesabı (veya Docker çalıştırabilen herhangi bir yer).
- GitHub hesabı.

### Telegram bot token nasıl alınır

1. Telegram'da **@BotFather**'a yazın.
2. `/newbot` yazın, botunuza bir isim ve kullanıcı adı verin.
3. Size verdiği token'ı kopyalayın (örn: `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).

### Chat ID nasıl alınır

1. Botunuzu Telegram'da bulun, `/start` yazın (veya botu bir gruba ekleyin).
2. Şu adrese tarayıcıdan gidin (TOKEN yerine kendi token'ınızı yazın):
   `https://api.telegram.org/botTOKEN/getUpdates`
3. Dönen JSON içinde `"chat":{"id": ...}` kısmındaki sayı sizin chat ID'niz.
   (Gruba eklediyseniz bu sayı negatif olabilir, aynen öyle kullanın.)

---

## 2. Railway'e deploy etme (GitHub üzerinden)

1. Bu klasörü kendi GitHub reponuza push edin:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/KULLANICI_ADINIZ/REPO_ADI.git
   git push -u origin main
   ```
2. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → az önce push ettiğiniz repoyu seçin.
3. Railway, projedeki `Dockerfile`'ı otomatik algılayıp imajı kendisi build eder — ekstra ayar gerekmez.
4. Railway panelinde **Variables** sekmesine girip şunları ekleyin:

   | Değişken | Değer |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | BotFather'dan aldığınız token |
   | `TELEGRAM_CHAT_ID` | Yukarıda bulduğunuz chat ID |
   | `RUN_MODE` | `paper` (önerilir) veya `live` |
   | `STRATEGY_PROFILE` | `balanced` (varsayılan; `conservative` / `aggressive` / `professional` da var) |

   `BINANCE_API_KEY` / `BINANCE_API_SECRET` **girmenize gerek yok** — sadece
   herkese açık veri kullanılıyor.

   **İsteğe bağlı ince ayar değişkenleri** (hiçbirini eklemezseniz seçtiğiniz
   `STRATEGY_PROFILE`'ın varsayılanları geçerli olur):

   | Değişken | Ne işe yarar | Örnek |
   |---|---|---|
   | `MIN_CONFIDENCE` | Sinyal üretmek için gereken minimum güven skoru (0-100) | `82` |
   | `MIN_QUOTE_VOLUME_USDT` | Taramaya dahil edilecek minimum 24s işlem hacmi | `5000000` |
   | `SCAN_INTERVAL_SECONDS` | İki tarama döngüsü arası bekleme süresi | `30` |
   | `MAX_SYMBOLS_TO_ANALYZE` | Stage 1'i geçen kaç sembolün derin analize gireceği (boş = sınırsız) | `100` |
   | `SIGNAL_COOLDOWN_MINUTES` | Aynı sembol için art arda sinyal üretilemeyecek süre | `60` |
   | `BINANCE_TIMEOUT_SECONDS` | Binance API istekleri için zaman aşımı | `10` |

5. Deploy tamamlanınca **Deployments → Logs**'tan botun döngüye girdiğini görebilirsiniz. İlk sinyal/rapor üretildiğinde Telegram'a mesaj gelir.

### Kalıcı disk (önemli)

Railway'de container'lar yeniden başladığında dosya sistemi sıfırlanabilir.
SQLite veritabanının (`data/platform.db`) ve yedeklerin kalıcı olması için
Railway projenizde bir **Volume** oluşturup `/app/data` yoluna bağlayın
(Railway panelinde **Settings → Volumes**). Bu adım atlanırsa bot yine
çalışır ve sinyal gönderir, ama her yeniden başlatmada geçmiş veriyi
kaybeder.

---

## 3. Yerelde çalıştırma / test

```bash
pip install -r requirements.txt
cp .env.example .env   # sonra .env içine kendi token/chat_id'nizi yazın
python main.py
```

Testleri çalıştırmak için:

```bash
pip install pytest pytest-asyncio aioresponses
pytest tests/ -v
```

Docker ile yerelde:

```bash
docker build -t binance-signal-bot .
docker run --env-file .env -v $(pwd)/data:/app/data binance-signal-bot
```

---

## 4. RunMode'lar

- **`paper`** (varsayılan): canlı piyasa verisiyle tarar, sinyal üretir, Telegram'a gönderir. Gerçek emir yok — zaten hiçbir modda yok.
- **`live`**: bu katmanda `paper` ile davranışça birebir aynı (bkz. `execution_modes/live.py` docstring'i) — platform tüm modlarda sinyal-only.
- **`backtest`**: uzun süre çalışan bir döngü değildir; `execution_modes/backtest.py`'deki `BacktestRunner`'ı doğrudan bir script/REPL'den çağırarak geçmiş bir sinyali geçmiş mum verisiyle test etmek için kullanılır.

---

## 5. Proje durumu ve bilinen eksikler

Ayrıntılı modül modül durum, ertelenen maddeler ve neden ertelendikleri için
[`PROJECT_STATUS.md`](./PROJECT_STATUS.md) dosyasına bakın — her ertelenen
madde ilgili modülün kendi dosyasında da (docstring içinde) belgelenmiştir,
kodda "TODO" bırakılmamıştır.

Özet: tüm 22 modülün objektif olarak uygulanabilir çekirdeği tamam.
Ertelenen maddeler (SRS'de formülü verilmeyen filtre skorlama, CPU/RAM
metrikleri için `psutil` gerekliliği, Binance'in tarihsel emir defteri
verisi sağlamaması gibi gerçek kısıtlar) botun uçtan uca çalışmasını
engellemez.

---

## 6. Mimari

```
config/           Pydantic şema, 4 katmanlı config birleştirme
system/           Exception hiyerarşisi, loglama, retry, error handler
core/              Domain modelleri (Signal, Trade, Report, ...)
infrastructure/    database/, binance/, telegram/, rate_limiter, watchdog
engines/           17 dosya — analiz pipeline'ı + Telegram/Reporting/Analytics/Production
execution_modes/   backtest.py + live.py
application/       composition_root.py (wiring) + main.py (entry point)
main.py            üst seviye çalıştırma dosyası
```
