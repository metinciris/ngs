# v0.4.83 UI Adapter Contract

Amaç: ileride tasarım tamamen değişse bile YZ, Selection, IGV, QC ve raporlama mantığının bozulmaması.

## UI yalnız bu normalize edilmiş görünüm modelini tüketmelidir

Hasta düzeyi örnek sözleşme:

```json
{
  "patient_link_id": "PT-...",
  "record_key": "run::sample::DNA",
  "sample_type": "DNA",
  "patient_summary": "...",
  "release_recommendation": "review",
  "qc_state": "review",
  "fusion_state": null,
  "history_count": 2,
  "local_note": "..."
}
```

Varyant düzeyi:

```json
{
  "record_key": "run::sample::DNA",
  "variant_key": "chr7|...|T|G",
  "gene": "EGFR",
  "change": "p.L858R",
  "ai": {
    "review_status": "report",
    "report_recommendation": "...",
    "rationale": "...",
    "confidence": "high",
    "igv_required": false
  },
  "gennext": {
    "selection_state": "selected|not_selected|unknown",
    "selection_source_verified": true
  },
  "local": {
    "report_decision": "reported|not_reported|pending",
    "note": "..."
  }
}
```

## UI'nın yapmaması gerekenler

- `review_status=report` gördüğü için rapor kutusunu otomatik işaretlemek.
- `Selection=0` gördüğü için RNA negatif kabul etmek.
- gen/protein metnine bakarak farklı teknik örnekleri birleştirmek.
- ham JSON'daki eksik alanı `false` olarak yorumlamak.
- bilinmeyen schema alanını silerek yeniden kaydetmek.

## Tasarım değişikliğinde minimum testler

1. Aynı varyantta YZ/Selection/yerel karar üçü farklı olabilir.
2. v1 ve v2 YZ dönüşleri aynı normalized view model'e gelir.
3. RNA boş Selection negatif görünmez.
4. `record_key` yanlışsa YZ notu başka hastaya taşınmaz.
5. `variant_key` eşleşmiyorsa YZ notu yanlış satıra bağlanmaz.
6. IGV bekleyen varyant rapora otomatik terfi etmez.
7. Eski karar geçmişi yeni UI'da okunur.
8. UI sekmesi kapatılıp açılsa da kaydedilmiş yerel not/karar korunur.