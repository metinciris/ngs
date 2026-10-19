# NGS Mutasyon Havuzu v0.4.83 — Mimari ve Sürdürülebilir Tasarım

Bu belge, uygulamanın yalnız bugün nasıl çalıştığını değil, ileride arayüz değişse bile klinik/teknik karar mantığının bozulmaması için hangi sınırların korunması gerektiğini tanımlar.

## 1. Ana ilke: veri, karar ve arayüz birbirinden ayrıdır

Uygulama üç katmana ayrılarak düşünülmelidir:

1. **Kaynak ve kanıt katmanı**: GenNext Clinical/SNV/CNV/Fusion/Selection, QC/MultiQC, IGV, Enlil ve eski DNA/RNA raporları.
2. **Normalizasyon ve karar destek katmanı**: teknik örnek kimliği, hasta bağlantısı, varyant kimliği, YZ dönüşü, QC/IGV durumu, geçmiş kararlar.
3. **Sunum katmanı**: Tkinter/ttk sekmeleri, hasta kartları, mutasyon listesi, raporlama ekranı.

Arayüz hiçbir zaman ham web HTML'sini veya YZ JSON'unu doğrudan klinik karar olarak yorumlamamalıdır. Ham kaynak önce normalize edilir; UI yalnız normalize edilmiş modeli gösterir.

Bu ayrım korunursa gelecekte Tkinter yerine başka bir masaüstü/web arayüzü kullanılabilir ve karar mantığı değişmeden kalır.

## 2. Değişmemesi gereken kimlikler

### Teknik örnek kimliği

`record_key = runUuid :: sampleUuid :: sampleType`

Bu anahtar teknik örneğin birincil kimliğidir. Görsel MP etiketi tek başına teknik örnek kimliği değildir.

- DNA ve RNA aynı hasta olsa bile ayrı teknik kayıttır.
- Rerun ayrı teknik kayıttır.
- Aynı MP metni görülse dahi UUID eşleşmeden aynı teknik çalışma kabul edilmez.

### Hasta bağlantısı

`patient_link_id` biyolojik hasta düzeyindeki bağdır. Teknik kayıtların üzerine yazılmaz; farklı DNA/RNA/rerun kayıtlarını aynı hastada göstermek için kullanılır.

### Varyant kimliği

Mümkün olduğunda:

`variant_key = chromosome | position | reference | alternative`

Caller tarafından kaçırılmış/manual incelemede koordinat güvenilir değilse sentetik ama benzersiz anahtar kullanılır:

`MANUAL_SCAN|GEN|REGION|LABEL`

Varyant eşleştirmesi yalnız gen/protein adına göre yapılmamalıdır.

## 3. Kaynak modüllerinin sorumlulukları

- `collector.py`, `browser_worker.py`, `browser_sniffer.py`: web kaynak toplama.
- `clinical_source.py`, `clinical_completion.py`: Clinical sayfası ve tamamlanma doğrulaması.
- `gennext_report_selection.py`, `gennext_selection_*`: GenNext Selection'ın okunması ve teknik kimlikle saklanması.
- `rna_qc_bundle.py`: RNA QC paketleme/normalizasyon.
- `igv_workflow_v0481.py`, `igv_capture_v1.py`, `web_igv_evidence.py`: IGV istek ve kanıt akışı.
- `patient_ngs_history.py`, `previous_report_summary.py`, `patient_dual_report.py`: önceki DNA/RNA sonuçları.
- `ai_export.py`, `ai_export_job.py`, `ai_export_worker.py`: YZ değerlendirme paketinin oluşturulması.
- `ai_review.py`: YZ dönüş sözleşmesi ve inceleme verisi.
- `review_workspace_store.py`: v0.4.83 YZ notu/kararlarının normalize edilerek saklanması.
- `review_workspace_archive.py`: yerel YZ/karar arşivi.
- `review_workspace_ui.py`: YZ/IGV çalışma alanının sunumu.
- `mutation_workspace.py`: varyant satırında YZ, GenNext Selection ve yerel rapor kararının yan yana fakat bağımsız sunumu.
- `patient_workspace.py`: hasta düzeyi YZ özeti ve klinik çalışma alanı.
- `db.py`: kalıcı yerel veri ve şema göçleri.
- `app.py`: sekmelerin/çalışma alanlarının orkestrasyonu; iş mantığı buraya gömülmemelidir.

## 4. v0.4.83 arayüz sözleşmesi

Ferah arayüz için tek ekranda her şeyi sıkıştırmak yerine görev tabanlı sekmeler kullanılır:

- POOL Özeti
- Hasta İnceleme
- DNA / Mutasyon
- RNA / Füzyon
- QC / Uyarılar
- IGV İstekleri
- IGV Karar / Kanıt
- YZ Özeti
- Raporlama
- Geçmiş / Arşiv

Yeni tasarım yapılırken bu sekme adları değişebilir; fakat aşağıdaki **semantik alanlar** kaybolmamalıdır:

- `ai_review_status`
- `ai_report_recommendation`
- `ai_rationale`
- `ai_confidence`
- `igv_required`
- `igv_reason`
- `gennext_selection_state`
- `local_report_decision`
- `pathologist_note`
- `decision_history`

## 5. Üç kararın bağımsızlığı

Aynı varyant için üç ayrı durum vardır ve birbirine otomatik çevrilmez:

1. **YZ önerisi** — karar desteği.
2. **GenNext Selection** — platformda gerçekten seçilmiş/rapora aday edilmiş teknik durum.
3. **Yerel patolog rapor kararı** — raporlandı / raporlanmadı / beklemede.

YZ'nin `report` demesi Selection kutusunu işaretlemez. Selection'ın seçilmiş olması YZ'nin otomatik `report` vermesine yol açmaz. Yerel rapor kararı diğer ikisini silmez.

Bu ayrım denetlenebilirlik için zorunludur.

## 6. DNA ve RNA aynı motoru paylaşır, aynı kuralı paylaşmaz

### DNA

Ana kanıt sınıfları:

- SNV/indel
- CNV
- TMB
- MSI
- HRD
- QC
- havuz içi varyant dağılımı/carry-over şüphesi
- IGV
- önceki DNA/RNA raporları
- GenNext Selection

### RNA

Ana kanıt sınıfları:

- gerçek füzyon adayları
- partner/gen/breakpoint bilgisi
- caller ve destek metrikleri
- FusionInspector/Arriba/STAR-Fusion gibi kaynakların bağlamı
- RNA QC ve hedef yeterliliği
- IGV/eşdeğer breakpoint doğrulaması
- Fusion Selection

**Boş Selection listesi RNA için analitik negatif sonuç değildir.** Geçerli endpoint/caller verisi ve yeterlilik kontrolü olmadan "negatif" sonucu türetilmez.

## 7. UI değişikliklerinde uyumluluk kuralı

Yeni arayüz aşağıdaki adapter sözleşmesine uymalıdır:

`Raw Source -> Normalizer -> Domain Review Model -> UI Adapter -> User Decision -> Audit Store`

UI katmanı:

- SQL tablo ayrıntısını bilmemeli,
- ham YZ JSON anahtarlarına bağımlı olmamalı,
- GenNext DOM seçicilerini bilmemeli,
- rapor kararını YZ kararından türetmemelidir.

Böylece yeni bir tasarım yalnız `UI Adapter` katmanını değiştirir.

## 8. Şema sürümleme

YZ dönüş dosyaları `schema_version` ile kabul edilir. v1 ve v2 geçmiş dosyaları normalize edilerek okunur. Yeni alan eklerken:

- mevcut alan kaldırılmaz,
- bilinmeyen alanlar yok sayılabilir ama kaybolmadan arşivlenir,
- varsayılan değer klinik karar üretmemelidir,
- migration işlemi idempotent olmalıdır.

## 9. Uzun süren YZ üretimi

v0.4.83'te paket üretiminin yalnız sabit 600 saniyeyi aşması başarısızlık sayılmaz. İş durumu/çıktı varlığı esas alınır. Kullanıcı iptali ve gerçek worker hataları ayrı olaylardır.

Gelecekte ideal durum makinesi:

`QUEUED -> COLLECTING -> NORMALIZING -> PACKAGING -> VALIDATING -> COMPLETED`

Hatalar:

`FAILED_RETRYABLE`, `FAILED_FATAL`, `CANCELLED`

Süre tek başına `FAILED` sebebi olmamalıdır.

## 10. Public/private sınırı

Public GitHub yalnız kaynak kodu ve sentetik test verisini içerir. Aşağıdakiler commit edilmez:

- hasta adı / TC / gerçek protokol kimliği,
- gerçek run/sample UUID,
- gerçek TSV/JSON/MultiQC çıktısı,
- `DATA/`, `OUTPUT/`, `AI_EXPORT/`, `PROFILE/`, `DIAGNOSTICS/`, `ledger.jsonl`,
- kullanıcıya özgü browser profile/cookie/token,
- `config.local.json` ve kurum içi özel URL/kimlik bilgileri.

Tam kaynak snapshot'ı bu sınırlar korunarak üretilmelidir.