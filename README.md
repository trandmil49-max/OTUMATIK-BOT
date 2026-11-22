# COLDE-BOT v9 — seçici BingX Futures botu

Amaç: günde az ama kaliteli işlem. Kumar yok: setup yoksa işlem yok.

## Nasıl çalışır
1. **Tarama evreni:** BingX Futures'taki tüm pariteler (kripto, altın, gümüş, petrol…). Kriptoda 24s hacmi `MIN_QUOTE_VOLUME` altındakiler elenir.
2. **Seviyeler:** 1g, 4s ve 1s grafiklerdeki dönüş noktalarından destek/direnç çıkarılır, her saat yenilenir.
3. **Alarm:** Fiyat bir seviyeye yaklaşınca o parite izleme listesine girer. Sadece o paritelere bakılır.
4. **Giriş:** Her 15 dk'lık mum kapanışından sonra (1s ve 15dk mumlarda) şu şartlar aranır:
   - seviyeye dokunuş ve **2 mum kapanışı** seviyenin doğru tarafında (seviye kapanışla kırılmışsa giriş yok)
   - 4s trende ters değil
   - fiyat seviyeden uzaklaşmamış (kovalama yok), spike mumu yok, 24s'te %15+ oynamamış
   - skor ≥ `MIN_SCORE` (seviye gücü, trend uyumu, hacim, risk/ödül)
5. **TP (tek):** bir sonraki karşı seviyenin %85'i (direncin hemen önü), en fazla 2 ATR. Karşı seviye çok yakınsa (yer yoksa) işlem yok.
6. **SL:** seviyenin ve dokunuş fitilinin ötesi.
7. **Korumada çıkış:** yolun %50'sinde stop girişe (komisyon payıyla) çekilir; %70'ten sonra kârın %35'i geri verilirse çıkar. 24 saatte sonuç yoksa çıkar.
8. **Günlük sınırlar:** en fazla `MAX_TRADES_PER_DAY` işlem (varsayılan 5), aynı anda 3 pozisyon, gün içinde üst üste 2 SL'de o gün durur, gün başı bakiyenin %3'ü kaybedilirse durur.
9. **Boyut:** 25$ ve altında işlem başına bakiyenin %1'i risk; 100$'da %2,5'e kadar yavaş artar. Kaldıraç: BTC/ETH ≤10x, volatil coinler 2-5x.
10. **Telegram:** işlem açıldı / TP / SL / korumada çıkış + günlük (23:00), haftalık (pazar), aylık (ay sonu) rapor.

## Modlar
- `paper`: gerçek BingX fiyatı, sahte para (komisyon + kayma hesaplı). **API anahtarı gerekmez.** İlk hafta bunu kullan.
- `vst`: BingX demo hesabı (sanal USDT). Bakiye çok büyük olduğu için `BALANCE_OVERRIDE=12` ver.
- `live`: gerçek para.

## İşlemleri BingX'te görmek için
`MODE=paper` hiçbir zaman BingX'e emir göndermez: sadece Telegram'a bildirim atar ve sahte bakiyeyi kendi veritabanında tutar. İşlemlerin BingX hesabında görünmesi için:
1. **BingX API anahtarı** oluştur (sadece *Futures işlem* izni, çekim izni YOK, mümkünse IP kısıtı koy).
2. Railway Variables: `BINGX_API_KEY`, `BINGX_API_SECRET`.
3. `MODE=vst` → BingX **demo hesabında** (sanal USDT) işlem açar. Demo bakiye büyük olduğu için `BALANCE_OVERRIDE=12` ekle; bot 12$'lık hesap gibi boyutlandırır ve kâr/zararı bakiyeye ekler.
4. Demo'da bir hafta sorunsuz çalışırsa `MODE=live` (gerçek para). `BALANCE_OVERRIDE`'ı sil.
Telegram mesajlarında mod etiketi görünür: `[PAPER]`, `[VST]`, canlıda etiket yok.

## Railway kurulumu
1. Bu klasörü GitHub'a yükle (dosya sayısı az, web upload yeter).
2. Railway → New Project → GitHub repo.
3. **Volume ekle**, mount path `/data`. (Olmazsa deploy'da veritabanı silinir.)
4. Variables: `.env.example` içindekileri gir (en az `MODE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DB_PATH=/data/colde.db`).
5. Deploy. Başlayınca Telegram'a "COLDE-BOT başladı" mesajı gelir. İlk seviye hesabı birkaç dakika sürer.

## Testler
```
pip install -r requirements-dev.txt
python -m pytest -q
```
## Bilinen sınır
Canlı emir uçları (`MODE=vst/live`) BingX dokümanına göre yazıldı ama geliştirme ortamından BingX'e erişilemediği için gerçek borsada denenmedi. Strateji, risk, yönetim ve paper akışı sentetik verilerle test edildi. Bu yüzden önce `paper`, sonra `vst`, en son `live`.
