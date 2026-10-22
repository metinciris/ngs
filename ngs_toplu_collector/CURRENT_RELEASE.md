# NGS Mutasyon Havuzu — geçerli çalışan sürüm

**Geçerli yerel uygulama:** v0.4.84 + Rapor Arşivi V4 (tek sayfa hasta görünümü)  
**Kullanıcı tarafından doğrulandı:** 22 Ekim 2026  
**Güncelleme paketi:** `NGS_UPDATE_v0.4.84_RAPOR_ARSIVI_V4_TEK_SAYFA.zip`  
**ZIP SHA-256:** `1b0568c7e8747675f6fea172cb184cc81cd2c9ac01523d68994ccbae6b234a0c`

## Geçerli kabul edilen arayüz

- Hasta isimleriyle başlayan, son POOL'dan eski POOL'a uzanan düz hasta listesi.
- DNA MP ve RNA MP ile birden çok çalışma aynı hasta görünümünde.
- Bir hastanın DNA/RNA ve varsa geçmiş raporları **aynı sayfada**, kendi tanılarıyla ayrı ayrı gösterilir.
- Hasta demografisi, tanı, blok, imzacı doktor ve DNA biyobelirteçleri (TMB, MSI, HRD) okunabilir.
- Üstteki arama/filtreler varsayılan olarak gizlidir.
- T.C. kimlik numarası yalnız yerelde hasta geçmişini ilişkilendirmek için kullanılır.
- Birden fazla teknik çalışmanın rapor metinleri birleştirilmez; güncel rapor ve eski rapor ayrımı korunur.

## Sürüm ve kaynak bütünlüğü hakkında

Bu belge, kullanıcı tarafından çalışan **son geçerli yama paketini** tanımlar. Bu depodaki `ngs_toplu_collector/VERSION` ve kaynak dosyaları V4 ZIP ile **henüz tam senkronize edilmemiş olabilir**. Paket tam kaynak dağıtımı değil, mevcut v0.4.84 kurulumuna ek güncellemedir. Eski depo kaynaklarını yeniden etiketleyip V4'ün tümü GitHub'daymış gibi göstermeyin.

Tam GitHub kaynak senkronizasyonu için kullanılan güncel yerel v0.4.84 kaynak ağacı ile bu ZIP'in modülleri birleştirilip test edilmelidir. Hasta veritabanı, T.C. kimlikleri, raporlar ve kişisel bilgiler hiçbir zaman GitHub'a yüklenmemelidir.
