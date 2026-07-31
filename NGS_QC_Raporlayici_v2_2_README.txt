NGS QC RAPORLAYICI v2.2 - HIBRIT QC
===================================

Bu sürüm iki veri kaynağını birlikte kullanır:

1. İlk nesil Altium QC klasörü
   - *.report.html
   - *.trimming.report
   - *.fq.fqStat.txt
   - desteklenen TXT/HTML/CSV dosyaları

2. Analiz sonrası indirilen MultiQC JSON ZIP paketleri
   - multiqc_data.zip
   - multiqc_data (1).zip
   - adı Windows tarafından değiştirilmiş diğer ZIP'ler

KULLANIM
--------
1. Eski HTML/TXT QC dosyalarını ve indirdiğiniz MultiQC ZIP'lerini aynı klasöre koyabilirsiniz.
2. ngs_qc_raporlayici_v2_2.pyw dosyasını çift tıklayın.
3. Klasör Seç düğmesiyle klasörü gösterin.
4. Program DNA ve RNA örneklerini ayrı tanır, aynı hastaya ait olanları birlikte gösterir.
5. Tüm QC Cümlelerini Kopyala düğmesi, hasta başlığı altında önce DNA sonra RNA paragrafını verir.

ÖNCELİK KURALI
--------------
- Aynı örnek için MultiQC ZIP mevcutsa okuma, Q30, GC, temiz veri ve post-alignment metriklerinde ZIP verisi kullanılır.
- ZIP yoksa eski HTML/TXT/trimming dosyalarındaki veriler kullanılır.
- Böylece MultiQC sayfası bozuk veya indirilemeyen RNA olgusu eski QC dosyalarıyla yine raporlanabilir.

DNA / RNA EŞLEŞTİRME
--------------------
- ZIP dosya adı kullanılmaz.
- Örnek kodu ve analiz kimliği ZIP içindeki JSON'dan okunur.
- MP94-26--MP112-26 gibi bir kimlikte DNA protokolü MP94/26, RNA protokolü MP112/26 olarak gösterilir.
- DNA ve RNA ham örnek numaraları farklı olsa bile aynı MultiQC analiz kimliği varsa aynı hasta altında birleştirilir.
- MultiQC bulunmayan eski dosyalarda örnek adının başındaki protokol numarası kullanılır.

RAPORLANAN QC METRİKLERİ
------------------------
- Paired-end okuma çifti
- Q30 baz oranı
- DNA için GC oranı
- Filtreleme sonrası temiz veri miktarı
- Ortalama kapsama derinliği
- Medyan kapsama derinliği
- En az 100x kapsanan hedef bölge oranı
- Duplikasyon oranı (mevcutsa)
- VerifyBAMID FREEMIX değeri (mevcutsa)

NOT
---
RNA MultiQC paketlerinde Picard duplikasyon metriği bulunmayabilir. Bu durumda duplikasyon cümlesi yazılmaz; diğer post-alignment metrikleri raporlanır.

GEREKSİNİM
----------
Python 3.10 veya üzeri. Harici Python paketi gerekmez.
