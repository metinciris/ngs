NGS QC RAPORLAYICI v2.4 - PROTOKOL ESLESTIRMELI HIBRIT QC
==========================================================

YENILIKLER
-----------
- Hasta numarasi artik tam protokol olarak gosterilir: MP95/26.
- DNA ve RNA rapor protokolleri ayri korunur:
    Hasta protokolu: MP95/26
    DNA rapor protokolu: MP95/26
    RNA rapor protokolu: MP114/26
- Tablo sutunlari "Hasta protokolu" ve "DNA/RNA protokolu" olarak duzenlendi.
- MP95-26--MP114-26 gibi MultiQC analiz kimlikleri otomatik cozumlenir.
- RNA veya DNA MultiQC ZIP'i eksikse, ayni ham numarali kardes kayittaki analiz kimligi kullanilir.
- ZIP bulunmayan temel QC kayitlarinda havuzdaki ortak yil kullanilir:
    96D -> MP96/26
    103R -> MP103/26
- 2026 ve 26 yil yazimlari raporda /26 olarak standartlastirilir.

ORNEK GORUNUM
--------------
Hasta protokolu | DNA/RNA protokolu | Tur
MP95/26         | MP95/26            | DNA
MP95/26         | MP114/26           | RNA

CALISMA SEKLI
-------------
1. Ilk nesil HTML/TXT/trimming dosyalarini ve MultiQC ZIP'lerini ayni klasore koyun.
2. ngs_qc_raporlayici_v2_4.pyw dosyasini cift tiklayin.
3. "Klasor Sec" dugmesiyle klasoru gosterin.
4. Program ham D/R numaralari ile MultiQC analiz kimliklerini eslestirir.
5. Her hasta altinda DNA ve RNA QC metinleri birlikte goruntulenir.

NOT
---
Havuz yiliyla tamamlanan protokol numaralari, MultiQC analiz kimligi bulunmayan temel QC kayitlarinda kullanilir. Farkli yillara ait dosyalar ayni klasore karistirilmamalidir.
