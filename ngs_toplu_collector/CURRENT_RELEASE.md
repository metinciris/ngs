# NGS Mutasyon Havuzu — geçerli çalışan yerel referans

**Kullanıcı tarafından onaylanan yerel kurulum:** v0.4.84 + Rapor Arşivi V5 (Selection görünümü) + Arşiv Sağ Panel Kaydırma V6  
**Onay tarihi:** 22 Ekim 2026  
**Yerel kurulum hedefi:** `C:\ngs\ngs_toplu_collector`  
**Durum:** **ÇALIŞAN REFERANS / KORUNACAK YEREL KURULUM**

## Bu kontrol noktasında onaylanan işlevler

- Rapor Arşivi / Hasta-Raporlar bölümünde hasta, DNA ve RNA teknik çalışma, GenNext Selection varyantları ve rapor alanları birlikte görüntülenir.
- Sağ hasta ayrıntısı paneli dikey kaydırılır; soldaki hasta listesi sabit kalır.
- Önceki arşiv işlevleri, hasta bağlantıları ve yerel rapor metinleri korunur.
- Kullanıcı, bu kurulumun arayüzünü “güzel gözüküyor” diyerek çalışan/iyi sürüm olarak işaretlenmesini istemiştir.

**Son yerel yamalar:**
- `NGS_UPDATE_v0484_RAPOR_ARSIVI_V5_SELECTION.zip`
- `NGS_UPDATE_v0484_ARSIV_SAG_PANEL_SCROLL_V6.zip`

## Onay kapsamı ve açık noktalar

Bu onay, yukarıdaki **yerel arşiv ekranı sürümüne** aittir. Tüm canlı GenNext Selection, RNA, IGV, otomatik tarama ve 404 kurtarma yollarının uçtan uca test edildiği anlamına gelmez. Özellikle eski/yanlış `run-detail-somatic` adresleri ile gerçek `somatic-detail-clinic` bağlantıları arasındaki eşleştirme sorunları için bağımsız doğrulama gerekir.

GenNext Selection, YZ önerisi ve patolog rapor kararı ayrıdır. Kimliği doğrulanmayan Selection, negatif olarak kaydedilmez. Kayıtlar `runUuid::sampleUuid::sampleType` üzerinden doğrulanır.

## GitHub kaynak eşitleme durumu

**DİKKAT: Henüz tam kaynak senkronizasyonu yapılmadı.** Kullanıcının Windows bilgisayarındaki en son çalışan kaynak ZIP'i alınmadan depodaki mevcut Python dosyaları ve `VERSION` bu referans sürümün eşleniği olarak kabul edilemez. Bu belge yalnız **yerel çalışan sürümün sabit referansını** işaretler.

Kaynak ZIP'i alındığında, gizlilik kontrolünden geçirilerek çalışır yerel kodla karşılaştırılmalı; Python sözdizimi/temel regresyon testleri sonrasında GitHub güncellenmelidir. Eski depo kodlarını yalnız sürüm numarasını artırarak son sürümmüş gibi göstermeyin.

## Gizlilik

GitHub'a hiçbir zaman gerçek hasta/örnek kayıtları, T.C. kimlik bilgileri, klinik raporlar, `AI_EXPORT`, `DATA`, `OUTPUT`, `PROFILE`, `DIAGNOSTICS`, `UPDATE_BACKUPS`, tarayıcı çerezleri/oturumları, kayıtlı hesaplar, şifreler, tokenlar veya yerel özel ayarlar yüklenmez.
