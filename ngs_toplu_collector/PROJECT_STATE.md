# PROJECT STATE — NGS Mutasyon Havuzu v0.4.83

**Latest validated source line:** 0.4.83  
**Focus:** POOL YZ değerlendirme merkezi, DNA/RNA ayrımı, YZ/Selection/yerel karar ayrıştırması, IGV/QC/önceki rapor bağlamı, ferah sekmeli UI, uzun YZ üretimlerinde sert 10 dakika kesintisinin kaldırılması.

Bu dosya geliştirmeye yeniden başlama noktasıdır. Tasarım değiştirilmeden önce `docs/ARCHITECTURE_v0483.md`, `docs/AI_DECISION_ENGINE_v0483.md` ve `docs/UI_ADAPTER_CONTRACT_v0483.md` okunmalıdır.

## Değişmez mimari sınırlar

- Teknik örnek: `runUuid::sampleUuid::sampleType`.
- `patient_link_id` hasta düzeyi bağlantıdır; teknik kimliğin yerine geçmez.
- `variant_key` varyant düzeyi çapraz referanstır.
- DNA ve RNA ortak hasta çalışma alanında gösterilebilir fakat analitik kuralları ayrıdır.
- GenNext Selection, YZ önerisi ve patolog/yerel rapor kararı üç ayrı veri alanıdır.
- YZ hiçbir zaman Selection'ı veya nihai rapor kararını otomatik değiştirmez.
- Empty RNA Selection analitik negatif değildir.
- QC/contamination heuristics tek başına kanıtlanmış kontaminasyon etiketi üretmez.
- Unknown oncogenicity sessizce düşürülmez.
- IGV bekleyen öneri, IGV sonucu olmadan otomatik rapora terfi etmez.

## v0.4.83 çalışma alanları

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

## YZ dönüş uyumluluğu

Geçmiş `ngs_ai_review_return_v1` ve `v2` dönüşleri normalize edilerek okunur. Yeni UI ham şemaya doğrudan bağlanmamalıdır.

## Uzun YZ üretimi

Eski 600 saniyelik sert deadline klinik paket tamamlanmak üzereyken yanlış başarısızlık oluşturabiliyordu. v0.4.83'te salt süre aşımı worker'ı başarısız saymaz; gerçek worker hatası/kullanıcı iptali ayrı tutulur.

## Public/private sınırı

Bu repo kaynak kodu ve sentetik test verisi içindir. Gerçek hasta/örnek/run verisi, AI_EXPORT, DATA, OUTPUT, PROFILE, DIAGNOSTICS, browser oturumları, yerel tokenlar ve kurum içi config commit edilmez.

## Sonraki geliştirme kuralı

Yeni özellik eklerken önce domain sözleşmesine alan ekle, sonra storage migration, sonra adapter, en son UI. UI'dan doğrudan DB/ham JSON/DOM karar mantığı eklemeyin.