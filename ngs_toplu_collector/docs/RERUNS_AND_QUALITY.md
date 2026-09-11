# Teknik Rerun ve Kalite Modeli

Laboratuvar konvansiyonu:
- `MP001-26` = bir teknik çalışma
- `MP001--26` = aynı vaka için tekrar yapılan ayrı teknik çalışma

Collector bunları ASLA tek örnek olarak birleştirmez.

Veritabanı:
- `patient_id`: teknik etiket (`MP001--26` korunur)
- `case_family`: biyolojik ilişki (`MP001-26`)
- `is_rerun`: 0/1
- `record_key`: runUuid + sampleUuid + DNA/RNA tipi

Analiz prensibi:
- varyant gözlemleri teknik örnek bazında tutulur
- VAF/AD/DP ve filtre durumu teknik örnek bazında tutulur
- QC (coverage, duplication, contamination, read quality vb.) aynı teknik örnekle eşleştirilir
- düşük kaliteli rerun veya ilk çalışma, iyi kalite tekrar ile karşılaştırılır
- kötü okumada artan düşük VAF çağrılar olası artefakt sinyali olarak ayrıca değerlendirilebilir

Bileşik etiketler (`MP002-26--MP003-26`) otomatik rerun kabul edilmez; orijinal etiket korunur.
