# v0.4.83 YZ Karar Yapısı ve Referans Formülü

Bu belge YZ'nin gizli düşünme sürecini tarif etmez. Programın dışarıdan denetlenebilir **karar sözleşmesini** tanımlar. Amaç, farklı YZ modelleri veya farklı arayüzler kullanılsa bile aynı güvenlik/öncelik sırasının korunmasıdır.

## 1. Klinik nihai karar YZ'ye ait değildir

YZ aşağıdaki sınırlı durumları üretir:

### Hasta düzeyi

`release_recommendation`:

- `ready`
- `review`
- `hold`
- `rerun`
- `indeterminate`

### Varyant düzeyi

`review_status`:

- `report`
- `consider`
- `igv_review`
- `hold`
- `do_not_report`
- `indeterminate`

### Güven

`confidence`:

- `high`
- `medium`
- `low`
- `indeterminate`

Bunlar **raporun kendisi değildir**. Nihai rapor kararı ayrıca saklanır.

## 2. Referans karar formülü

Sayısal tek bir "kanser skoru" kullanılmaz. Karar, önceliklendirilmiş kapı (gate) mantığıyla üretilir:

```text
YZ_ÖNERİSİ =
    HARD_GUARDRAILS(
        teknik_kimlik,
        analiz_tamamlanma,
        kaynak_bütünlüğü,
        örnek_türü
    )
    -> TEKNİK_YETERLİLİK(QC, kontaminasyon/karışım sinyalleri)
    -> MOLEKÜLER_KANIT(DNA veya RNA)
    -> IGV_DURUMU
    -> KLİNİK_BAĞLAM
    -> ÖNCEKİ_RAPORLAR
    -> ÖNERİ
```

"->" burada önceki basamağın sonraki basamağa sınır koyduğu anlamına gelir. Alt basamak üstteki güvenlik engelini aşamaz.

## 3. Hard guardrail önceliği

Aşağıdaki koşullardan biri varsa otomatik `report` önerisi üretilemez:

```text
IF teknik_kimlik eşleşmiyor
    => HOLD
ELSE IF run/sample analizi tamamlanmamış
    => HOLD
ELSE IF doğru DNA/RNA kaynağı doğrulanamıyor
    => HOLD veya INDETERMINATE
ELSE
    => teknik yeterlilik değerlendirmesine geç
```

Özellikle MP etiketi benzerliği teknik UUID eşleşmesinin yerine geçmez.

## 4. Teknik yeterlilik kapısı

QC tek metrikle PASS/FAIL yapılmamalıdır. Kullanılan yapı:

```text
TECHNICAL_ADEQUACY = combine(
    depth/coverage,
    read support,
    duplication,
    alignment/assignment,
    target adequacy,
    assay controls,
    FREEMIX/VerifyBAMID veya eşdeğer karışım göstergeleri,
    run-specific warnings
)
```

Bir karışım göstergesi yüksek diye "kontaminasyon kanıtlandı" sonucu verilmez. Sonuç yalnız risk/öncelik üretir.

Öneri önceliği:

```text
kimlik/karışım riski kritik          => RERUN/HOLD
QC kırılgan + düşük destek           => HOLD/IGV_REVIEW
QC yeterli fakat belirsiz kanıt      => REVIEW/CONSIDER
QC yeterli + güçlü doğrulanmış kanıt => sonraki kapıya geç
```

## 5. DNA karar mantığı

Varyant için normalize edilmiş kanıt seti:

```text
DNA_EVIDENCE = {
  GenNext Selection,
  PASS/non-PASS,
  AD/DP,
  VAF,
  consequence,
  oncogenicity,
  CancerVar/ClinVar/OncoKB gibi anotasyonlar,
  exact-variant pool spread,
  recurrent technical signals,
  IGV,
  CNV/TMB/MSI/HRD bağlamı,
  eski DNA/RNA sonuçları
}
```

Referans kural:

```text
IF artifact kanıtı güçlü
    => DO_NOT_REPORT
ELSE IF onkojenisite unknown
    => CONSIDER + unknown_oncogenicity etiketi; sessiz düşürme YOK
ELSE IF read desteği/QC kırılgan veya non-PASS teknik sorun var
    => IGV_REVIEW veya HOLD
ELSE IF klinik olarak anlamlı + teknik olarak destekli
    => REPORT adayı
ELSE
    => CONSIDER / INDETERMINATE
```

`REPORT adayı` nihai patolog raporu değildir.

## 6. Havuz içi yayılım / carry-over mantığı

Program, bir varyantın farklı örneklerdeki dağılımını teknik uyarı olarak kullanabilir:

```text
HIGH_TO_LOW_SIGNAL =
    source_sample yüksek VAF
    AND recipient_sample çok düşük VAF
    AND exact aynı variant_key
```

Birden fazla ortak varyant aynı kaynak/alıcı çiftinde görülüyorsa pair signature güçlenir.

Ancak:

```text
HIGH_TO_LOW_SIGNAL != kanıtlanmış kontaminasyon
FREEMIX yüksek       != tek başına kanıtlanmış kontaminasyon
aynı hotspot iki hastada != tek başına carry-over
```

Karar en fazla `review`, `hold`, `rerun` veya `igv_review` önceliğini yükseltir.

## 7. RNA karar mantığı

RNA için en önemli güvenlik kuralı:

```text
Selection boş => negatif değildir
```

Negatif/pozitif yorum için en azından aşağıdaki zincirin doğrulanması gerekir:

```text
SOURCE_VALID
AND ANALYTICAL_FUSION_CANDIDATES_RESOLVED
AND RNA_QC_ADEQUATE
AND TARGET/PANEL_SCOPE_KNOWN
```

Pozitif aday için:

```text
candidate identity
+ fusion partners
+ breakpoint/frame
+ caller support
+ junction/read evidence
+ gerektiğinde IGV/eşdeğer doğrulama
```

Eksikse `indeterminate`/`hold` korunur; YZ boşluğu negatiflikle doldurmaz.

## 8. IGV kuralı

`igv_required=true` ise aynı teknik örnek ve aynı `variant_key` için IGV isteği oluşturulur.

```text
record_key + variant_key
```

çapraz referans anahtarıdır.

IGV sonucu yoksa YZ'nin "IGV sonrası değerlendir" önerisi otomatik olarak `report` durumuna yükselmez.

## 9. GenNext Selection ile YZ ilişkisi

```text
YZ_REPORT =/= GENNEXT_SELECTED
GENNEXT_SELECTED =/= FINAL_REPORTED
YZ_ACCEPTED =/= FINAL_REPORTED
```

Bu üç durum ayrı sütunlarda ve ayrı audit kayıtlarında tutulur.

## 10. Nihai karar formülü

Program açısından nihai rapor durumunun referans formülü:

```text
FINAL_REPORT_DECISION = PATHOLOGIST_DECISION(
    YZ_advice,
    GenNext_selection,
    verified_molecular_evidence,
    QC,
    IGV,
    clinical_context,
    previous_reports
)
```

Burada `PATHOLOGIST_DECISION` insan onayıdır. YZ bunu otomatik hesaplamaz.

## 11. Karar geçmişi

Her değişiklikte mümkünse şu olay kaydı tutulur:

```text
{
  timestamp,
  record_key,
  variant_key?,
  source: AI | GENNEXT | LOCAL_USER | IGV,
  old_state,
  new_state,
  note?,
  schema_version
}
```

Bu yapı, tasarım değişse bile "YZ ne demişti / platformda ne seçilmişti / patolog ne karar verdi" sorularının ayrı yanıtlanmasını sağlar.