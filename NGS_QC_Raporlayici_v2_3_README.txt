NGS QC RAPORLAYICI v2.3 - HASTA GRUPLU HIBRIT QC
=================================================

KULLANIM
--------
1. İlk nesil Altium QC dosyalarını ve/veya MultiQC JSON ZIP paketlerini aynı klasöre koyun.
2. ngs_qc_raporlayici_v2_3.pyw dosyasını çift tıklayın.
3. "Klasör Seç" düğmesiyle klasörü gösterin.
4. Tabloda bir DNA veya RNA satırını seçin.
5. Sağdaki QC alanında aynı hastaya ait DNA ve RNA birlikte gösterilir.
6. "Seçili Hastanın QC Metnini Kopyala" ile iki paragrafı tek seferde kopyalayabilirsiniz.

HASTA VE PROTOKOL EŞLEŞTİRMESİ
------------------------------
- Eski sistem: 94D ve 94R gibi aynı ham numaralı DNA/RNA örnekleri aynı hasta altında gruplanır.
- Yeni sistem: MP94-26--MP112-26 gibi analiz kimliğinde iki rapor protokolü varsa:
  * DNA rapor protokolü: MP94/26
  * RNA rapor protokolü: MP112/26
  * Ortak hasta başlığı: Hasta 94
- DNA ve RNA ham örnek numaraları farklı olsa bile ortak çift protokol kimliği varsa aynı hastaya bağlanır.

QC DURUMU
---------
- Baz korunma oranı artık zorunlu metrik değildir; MultiQC ZIP paketlerinde bulunmaması eksiklik sayılmaz.
- "Tam + post-alignment": temel okuma metrikleri ve analiz sonrası metriklerden en az biri mevcut.
- "Tam (temel QC)": okuma, Q30, temiz veri ve DNA için GC mevcut; MultiQC post-alignment verisi yok.
- "Eksik: ...": rapor cümlesi için gerekli temel metriklerden biri bulunamadı.

DESTEKLENEN VERİLER
-------------------
- Zebra/FASTQ HTML veya TXT raporları
- Cutadapt .trimming.report dosyaları
- fqStat TXT dosyaları
- MultiQC JSON ZIP paketleri
- CSV/TSV özetleri
- ZIP ve uygun arşiv aracı varsa RAR

ÖNCELİK
-------
Aynı örnek için MultiQC verisi bulunursa okuma, Q30, GC, temiz veri ve post-alignment metriklerinde MultiQC önceliklidir. MultiQC bulunmazsa eski QC dosyaları kullanılır.

NOT
---
Program yalnızca Python standart kütüphanesini kullanır. Python 3.10 veya üzeri önerilir.
