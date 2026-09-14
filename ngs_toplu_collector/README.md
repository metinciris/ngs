# NGS Mutasyon Havuzu — v0.4.1

Yerel NGS veri toplama, mutasyon/POOL teknik inceleme, YZ raporlama desteği, IGV kuyruğu, hasta/rapor arşivi ve araştırma çalışma alanı.

> Public-source edition: kurum/vendor adresleri, kimlik bilgileri, hasta verileri, tarayıcı profili ve sekans çıktıları repoya dahil edilmez.

## v0.4.1 — hasta kimliği / çoklu tümör modeli

- Geçerli TC Kimlik No girildiğinde sistem **yalnız yerel SQLite arşivinde** aynı TC'yi arar.
- Eşleşme varsa **ad-soyad, doğum tarihi ve cinsiyet** hasta düzeyinde otomatik doldurulur; kullanıcı daha sonra manuel düzeltebilir.
- TC geçerli olsa bile sistem dış bir nüfus/kimlik servisine bağlanmaz; kontrol yalnız format/checksum ve yerel kayıt eşleşmesidir.
- Aynı kişinin **birden fazla tümörü/vakası** olabilir. Tanı, istem/raporlama doktoru ve vaka notu tümör/vaka düzeyinde kalır; başka tümöre otomatik taşınmaz.
- Her tümör/vaka altında **DNA, RNA, tekrar DNA ve tekrar RNA** teknik örnekleri ayrı kayıt olarak korunur. Rerunlar ve farklı assay'ler birleştirilmez.
- Blok, tümör yüzdesi ve teknik not teknik örnek düzeyindedir.
- Hasta/Rapor arşivi aynı TC ile güvenli biçimde bağlanan farklı vakaları tek kişi altında gösterebilir; teknik örnek ve raporlama kararları yine ayrı kalır.

## Ana çalışma alanları

### 1. Toplama / YZ / Raporlama

- Playwright tabanlı kalıcı Chromium profili ve RUNS taraması.
- DNA/RNA POOL gruplama; tamamlanmamış upstream işler WAITING olarak kalır.
- Clinical, SNV/ShortINDEL, CNV, Fusion ve QC toplama.
- Mutation Viewer, manuel `reported / not_reported / artifact` kararları.
- POOL içinde exact ortak varyant, high→low ve tüm-SNV teknik fingerprint incelemesi.
- YZ/raporlama paketi: TMB/MSI/HRD, QC/MultiQC tabloları, SNV/CNV/Fusion ve kaynak dosya envanteri.
- `AI_REVIEW_RETURN.json` geri dönüşü ve kalıcı IGV inceleme kuyruğu.
- Vaka notuna yapıştırılan final rapor metninden raporlanan varyant adaylarını çıkarma; ham SNV/CNV ile eşleştirme ve **manuel onay** sonrası rapor geçmişine ekleme.

### 2. Hasta Arşivi / Araştırma

- POOL seçmeden tüm hastalar, tümör/vakalar ve teknik çalışmalar.
- Raporlanan varyant geçmişi, rapor metni sürümleri, artefakt hafızası ve karar çelişkileri.
- Aynı varyantın hangi hastalarda/MP'lerde/POOL'larda görüldüğünü inceleme.
- Tanı, gen, protein/cDNA, tarih, yaş, cinsiyet, DNA/RNA, rerun, VAF, DP, PASS ve rapor durumu ile araştırma filtresi.
- Kaydedilebilir kohortlar ve pseudonymous CSV/manifest araştırma çıktısı.

## Veri güvenliği

Aşağıdakiler **local-only** ve git-ignored kalmalıdır:

- `DATA/` — SQLite, kimlik bağlantı anahtarı ve yedekler
- `OUTPUT/` — indirilen analiz çıktıları
- `PROFILE/` — Chromium oturumu/cookies
- `AI_EXPORT/`, `IGV_SNAPSHOTS/`, `ARCHIVE_EXPORT/`, `RESEARCH_EXPORT/`, `DIAGNOSTICS/`
- `ledger.jsonl`, `config.local.json`

TC YZ paketine veya araştırma dışa aktarımına yazılmaz. Hasta/doktor adları YZ paketinde maskelenir. Genomik/klinik pseudonymous veri yine hassas sağlık verisidir.

## Kurulum / güncelleme

Windows + Python 3.10+.

Yeni kurulumda `config.example.json` → `config.local.json`; özel `base_url` / `runs_url` yalnız yerel dosyada tutulur. Ardından `KURULUM.bat`, sonra `BASLAT.bat`.

Mevcut kurulumda uygulamayı kapatın, klasörünüzü yedekleyin ve v0.4.1 ZIP içindeki `ngs_toplu_collector` içeriğini mevcut klasörün üzerine çıkarın. `DATA/OUTPUT/PROFILE` kaynak ZIP'e dahil değildir ve silinmemelidir.

## GitHub'daki exact v0.4.1 kaynak

Bu repodaki **otoritatif v0.4.1 kaynak kopyası** şu klasördedir:

`ngs_toplu_collector/source_snapshot/v0.4.1/`

`RESTORE_SOURCE.bat` çalıştırıldığında Base64 parçaları birleştirilir ve exact source-only ZIP oluşturulur. Script SHA256 doğrular:

`7940602b695ba6e4136acb080b7a067e4f1ab6bdffff0b8a0e6fcbed5d8fe071`

Kök dizindeki okunabilir eski `.py` dosyalarının tamamı doğrudan v0.4.1'e senkronize edilene kadar **v0.4.1 için snapshot esas alınmalıdır**. Kök `source_snapshot_restore.bat` en güncel snapshot restore işlemini başlatır.

## Test

v0.4.1 source ZIP üzerinde:

- `python self_test.py`
- `python -m unittest test_archive test_patient_identity -v`

sentetik verilerle çalıştırıldı. Hasta TC lookup / manuel düzeltme, çoklu tümör, DNA/RNA/rerun ayrımı, arşiv ve araştırma regresyonları test edildi.

Raporlama, artefakt ve kontaminasyon işaretleri karar destek bilgisidir; nihai klinik değerlendirme değildir. Eksik kayıt “negatif” kabul edilmez.
