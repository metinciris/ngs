# POOL Oncogenic / Likely Oncogenic Yayılım Analizi

## Amaç
Aynı POOL içindeki teknik örneklerde tam SNV TSV üzerinden Oncogenic ve Likely Oncogenic çağrıların dağılımını incelemek.

## Neden tüm Filter değerleri tutulur?
Kontaminasyon veya düşük düzey carry-over, kaynak örnekte PASS/yüksek VAF iken alıcı örnekte MinAF, LowQual veya başka bir filtre ile görülebilir. Bu nedenle analiz indeksi PASS dışı çağrıları da içerir.

## İlk paternler
- TEK ÖRNEK: varyant POOL içinde bir teknik örnekte görülür.
- ORTAK: aynı exact chr/start/ref/alt en az iki teknik örnekte görülür.
- YÜKSEK→DÜŞÜK: en yüksek AF >= %5, ikinci AF <= %5 ve oran >=4x. İnceleme sinyalidir.
- ÇOKLU YAYILIM: aynı kaynak→alıcı yönünde en az iki farklı Oncogenic/Likely varyant YÜKSEK→DÜŞÜK paterni gösterir. Daha güçlü teknik inceleme sinyalidir.

## Sınırlar
Hotspot varyantlar farklı gerçek tümörlerde bağımsız oluşabilir. Çoklu POOL tekrarı otomatik artefakt anlamına gelmez. Bulgular histoloji, QC, tümör yüzdesi, rerun ve IGV ile birlikte değerlendirilmelidir.

## v0.2.2 arşiv geçişleri

`Varyant Yayılımı` tablosunda bir varyanta çift tıklamak aynı exact varyantın tüm arşivdeki hasta / teknik örnek gözlemlerini açar. Sağ tık menüsü OncoKB ve Franklin'e geçiş sağlar.

`Tüm Arşiv / Mutasyon Ara` ekranında mutasyon seçildiğinde alt tablo otomatik olarak bütün hasta gözlemlerini gösterir. Gen / değişiklik / varyant başlıkları `🔎` ile işaretlenmiştir.
