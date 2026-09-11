# YZ / Raporlama POOL dışa aktarımı

Bu modül, yerel olarak toplanmış NGS verilerinden seçilen bir POOL için YZ değerlendirmesine uygun iki katmanlı paket üretir.

## Üretilen dosyalar

- `POOL_AI_COMPACT.json`: POOL özeti, hasta/teknik örnek bağlamı, QC özeti, biyobelirteçler, Oncogenic/Likely Oncogenic varyantlar ve POOL içi ortak/yüksek→düşük varyant sinyalleri.
- `POOL_AI_FULL.json`: compact içeriğe ek olarak, varsa dışa aktarılmış MultiQC JSON verilerinin tamamını içerir.
- `POOL_AI_PROMPT.md`: YZ'ye yapıştırılabilecek Türkçe değerlendirme istemi.
- `MANIFEST.json`: dosya adları, üretim zamanı ve SHA256 değerleri.

## POOL teknik inceleme

Exact varyant anahtarı teknik örnekler arasında karşılaştırılır. Sistem:

- aynı varyantın birden fazla örnekte görülmesini,
- yüksek AF'lı olası kaynak ile düşük AF'lı eşleşmeleri,
- bir örnek çiftinde birden fazla ortak Oncogenic/Likely Oncogenic varyant bulunmasını

teknik inceleme sinyali olarak gösterir.

Bunlar otomatik kontaminasyon tanısı değildir. Hotspot biyolojisi, tümör yüzdesi, DP/AD, filtre durumu, rerun ve QC/FREEMIX ile birlikte patolog tarafından değerlendirilmelidir.

## Hasta bazlı içerik

Her teknik örnekte mümkün olan ölçüde:

- POOL / MP / DNA-RNA / rerun,
- tanı anı yaşı ve tümör tanısı,
- blok ve tümör hücre oranı,
- TMB / MSI / HRD,
- QC ana metrikleri,
- Clinical özet,
- tüm Oncogenic ve Likely Oncogenic varyant gözlemleri (PASS dışı düşük düzey çağrılar dahil),
- varsa manuel raporlama kararı

yer alır.

## Gizlilik

`Ad Soyad + TC dahil et` varsayılan olarak kapalıdır. `AI_EXPORT/` `.gitignore` içindedir ve public repoya gönderilmemelidir. De-identifiye NGS/tanı verisi de sağlık verisidir; yalnız kurumca onaylı YZ akışında kullanılmalıdır.

## Karar sınırı

Üretilen paket karar destek içindir. Nihai kontaminasyon yorumu, varyant sınıflaması ve klinik rapor patolog doğrulaması gerektirir.
