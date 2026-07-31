NGS QC RAPORLAYICI v2.1 - MULTIQC ZIP
=====================================

Bu sürüm yalnızca analiz sonrası indirilen MultiQC JSON ZIP paketlerini kullanır.
Eski report.html, trimming.report ve fqStat.txt dosyalarına ihtiyaç yoktur.

KULLANIM
--------
1. Her hastanın MultiQC ZIP paketini aynı klasöre indirin.
2. ZIP adlarının aynı olması sorun değildir. Windows bunları multiqc_data.zip,
   multiqc_data (1).zip, multiqc_data (2).zip şeklinde adlandırabilir.
3. ngs_qc_raporlayici_v2_1.pyw dosyasını çift tıklayın.
4. "Klasör Seç" ile ZIP'lerin bulunduğu klasörü gösterin.
5. Program örnek/protokol kimliğini ZIP içindeki JSON verisinden alır.

ZIP'TEN ALINAN METRİKLER
------------------------
- Paired-end okuma çifti
- Q30 baz oranı
- GC oranı
- Filtreleme sonrası temiz veri miktarı
- Ortalama kapsama derinliği
- Medyan kapsama derinliği
- En az 100x kapsanan hedef bölge oranı
- Duplikasyon oranı
- VerifyBAMID FREEMIX değeri

DNA VE RNA
----------
- D ile biten örnekler DNA, R ile biten örnekler RNA olarak tanınır.
- Aynı hastada DNA ve RNA birlikte bulunabilir.
- Mosdepth, Picard ve VerifyBAMID metrikleri varsa DNA paragrafına eklenir.
- RNA ZIP'inde yalnız okuma kalite metrikleri varsa RNA paragrafı bunlarla hazırlanır.

NOT
---
MultiQC paketinde eski trimming raporundaki baz korunma oranı bulunmadığından
bu cümle kaldırılmıştır. Bunun yerine analiz sonrası kapsama ve duplikasyon
metrikleri raporlanır.

Program yalnızca Python standart kütüphanesini kullanır.
Python 3.10 veya üzeri önerilir.
