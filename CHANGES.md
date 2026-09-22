# V8.0 — Telegram / Risk / Sinyal Kalitesi + Config Env Var Düzeltmesi

İki turda yapılan işi birleştiren tek paket. **21 dosya** değişti; SRS mimarisi,
veritabanı şeması ve diğer modüller olduğu gibi korundu. 465 orijinal test hâlâ
geçiyor, üzerine **41 yeni test** eklendi (toplam **506/506 yeşil**), lint temiz.

Doğrulama yöntemi: her iki turda da değişiklikler önce çalışma kopyasında
yapıldı, sonra SADECE bu 21 dosya, ZIP'in temiz bir kopyasının üzerine tekrar
uygulanıp test paketi orada da baştan çalıştırıldı — yani bu paket GitHub'a
uygulandığında da aynı sonucu vereceği doğrulandı, sadece "çalışma dizinimde
çalıştı" değil.

---

## TUR 1 — Telegram, duplicate bildirim, kaldıraç, confidence (2. metin)

### 1. Telegram bildirimleri — tam yeniden tasarım (`engines/telegram_notifications.py`)

14 bildirim tipinin tamamı tek ortak iskelete oturdu: başlık ikonu → sembol/yön
→ sayısal içerik (gruplu) → 🕒 saat. İstenen emoji eşlemesi uygulandı. Saatler
artık `General.timezone` (Europe/Istanbul) içinde gösteriliyor.

Yeni eklenenler (önceden hiç yoktu): `notify_bot_started()` / `notify_bot_stopped()`,
`notify_warning()` (SRS Part 18'in WARNING/ERROR bandı artık Telegram'a düşüyor,
`notification_level` ile kısılabilir). Rapor mesajları artık zaten hesaplanan
Toplam PNL/Win Rate/Best-Worst Trade'i emoji ile gösteriyor.

### 2. Duplicate bildirim düzeltmesi (`execution_modes/live.py`)

Kök neden: TP2/SL'de hem özel "hit" pingi hem `notify_trade_closed` art arda
gönderiliyordu. Artık sadece `notify_trade_closed` (durum zaten hangi seviyeden
kapandığını gösteriyor).

### 3. Kaldıraç — confidence tabanlı, maksimum 10x (`engines/risk_management.py`, `engines/signal_generation.py`)

ATR-tabanlı (1x-20x) sistem kaldırıldı. Yeni tablo:

| Confidence | Kaldıraç |
|---|---|
| 95+ | 10x |
| 90–94 | 8x |
| 85–89 | 7x |
| 80–84 | 5x |
| 75–79 | 3x |
| <75 | Sinyal üretilmez |

Kaldıraç artık confidence hesaplandıktan SONRA ayrıca hesaplanıyor (mimari not
dosyaların docstring'lerinde).

### 4. Confidence skorlaması (`engines/confidence.py`)

Risk/Bitcoin/Coin Trust/Market Health artık lineer değil üstel (üs=1.5)
ölçekleniyor — vasat skor artık tam kredi almıyor, mükemmel skor (100/100) hâlâ
tam puan alıyor.

### Bilerek DEĞİŞTİRİLMEYEN (gerekçeli)

Risk Management (SL/TP) zaten ATR+yapı tabanlı, "basit sabit hesaplama"
değildi. RSI/EMA/ADX/volume_ratio kodda var ama coin-seviyesinde sinyale hiç
bağlanmamış — gerçek bir boşluk ama kapsamı büyük (3 motor + confidence
dağılımının yeniden dengelenmesi), önceliğiniz düşüktü, bu turda yapılmadı.

---

## TUR 2 — Railway env var mapping bug'ı (eski sohbette yarım kalan)

Eski sohbette `config/loader.py` inceleniyordu: Railway'e girilen
`MIN_CONFIDENCE`, `MIN_QUOTE_VOLUME_USDT`, `MAX_SYMBOLS_TO_ANALYZE`,
`SCAN_INTERVAL_SECONDS`, `SIGNAL_COOLDOWN_MINUTES` değişkenlerinin **hiçbirinin
config'e okunmadığı** kesinleşmişti; konuşma çözüm uygulanmadan yarım kalmıştı.
Bu turda gerçekten düzeltildi:

### Neyin gerçek karşılığı vardı (sadece mapping eklendi)

- `MIN_CONFIDENCE` → `confidence.minimum_confidence`
- `MIN_QUOTE_VOLUME_USDT` → `scanner.min_24h_quote_volume_usdt`
- `SCAN_INTERVAL_SECONDS` → `scanner.fast_scan_interval_seconds` (varsayılanı gerçekten 30 — rate limiter sıkışmasının sebebiydi)

Pydantic'in string env değerini otomatik doğru tipe (int/float) çevirdiğini
gerçekten test ederek doğruladım — eski sohbetin yarım kalan sorusuydu.

### Neyin HİÇ karşılığı yoktu (yeni config alanı + gerçek davranış eklendi, sadece mapping değil)

- **`MAX_SYMBOLS_TO_ANALYZE`**: `ScannerConfig.max_symbols_to_analyze` (yeni
  alan, varsayılan `None`=sınırsız — mevcut davranışı bozmaz). Artık gerçekten
  `scanner_orchestrator.py`'de Stage 1 hayatta kalanlarını kırpıyor (Stage 1
  raporlaması etkilenmiyor, sadece Stage 2'ye kaç sembol gittiği). Sıralama
  korunuyor, hacme göre yeniden sıralama yapmadım (belirtilmemişti).
- **`SIGNAL_COOLDOWN_MINUTES`**: `RiskConfig.signal_cooldown_minutes` (yeni
  alan, varsayılan `None`=kapalı). Bir sembolün en son sinyalinden (yön fark
  etmez, hangi sonuçla bittiği fark etmez) bu kadar dakika geçmeden yeni sinyal
  üretilmiyor. Yeni `RejectionReason.SIGNAL_COOLDOWN` eklendi. Yön-bağımsız
  yaptım (LONG stop olduktan hemen sonra SHORT'a girmek de önlenmesi gereken
  durum).

İkisi de **opt-in** (varsayılan kapalı) — Railway'de bu değişkenleri hiç
girmemiş olan bir deploy için davranış hiç değişmiyor; sadece siz değeri
girdiğinizde artık gerçekten etkili oluyor.

### Test edilenler

`config/loader.py` için 4 yeni test (tip dönüşümü, varsayılan sınırsız,
geçersiz değerde yüksek sesle hata), `scanner_orchestrator.py` için 4 yeni test
(kırpma davranışı, sınırsız varsayılan, sınır survivor sayısından büyükse
no-op, sıralama korunuyor), `signal_generation.py` için 5 yeni test (cooldown
penceresinde red, yön-bağımsız, pencere geçince izin, kapalıyken etkisiz, farklı
sembolü etkilememesi), `signal_repository.py` için 2 yeni test.

---

## Bu paketle de ÇÖZÜLMEYEN, bilmeniz gereken bir şey yok

Tur 1'in sonundaki "kapsam dışı" notu (env var bug'ı) artık çözüldü. Sinyal
kalitesi tarafındaki RSI/EMA/ADX/volume_ratio bağlama konusu hâlâ ayrı bir görev
olarak duruyor — isterseniz onu da ele alalım.
