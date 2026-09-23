NGS Mutasyon Havuzu - Web IGV Evidence hotfix v0.4.34
========================================================

AMAÇ
----
Bu sürüm v0.4.33'teki yerel BAM/CRAM + BED pileup yaklaşımını aktif kullanım dışına alır.
Laboratuvarda BAM/BAI/BED dosyalarına hızlı ve güvenilir erişim olmadığı için varsayılan ve zorunlu çalışma modu:

  read_evidence_mode = web_igv_only

Program BAM / BAI / CRAM / BED dosyası aramaz, P2P'den indirmeye çalışmaz ve yerel pileup başlatmaz.
Klasik POOL Clinical/SNV/CNV/QC/Fusion çıktıları aynen kullanılmaya devam eder.

WEB IGV'DEN NE ALINIR?
----------------------
GenNext'te zaten açılmış/render edilmiş IGV sayfasından Playwright ile:
- IGV genome build (sayfanın gösterdiği değer)
- görünür locus ve pencere büyüklüğü
- hedef koordinatın görünür pencerede olup olmadığı
- track envanteri, label, loading/render durumu
- alignment / SNV-VCF / Targeted-Padding / Read-Depth track varlığı
- mümkün olduğunda VCF, Target/Padding ve Read Depth tracklerine hedef koordinatta click-probe
- popup gerçekten açılırsa popup'ın ham metni + key/value alanları
- alignment üzerinde sınırlı sayıda read popup probe denemesi
- mevcut Duplicate Reads / Sort by base hazırlık durumu

TOPLANMAZ / TAHMİN EDİLMEZ
--------------------------
- BAM/BAI/BED indirilmez.
- Canvas pikselinden DP/AD/VAF veya read sayısı tahmin edilmez.
- Web IGV exact pileup API/verisi açıkça göstermiyorsa:
    exact_pileup = false
    exact_pileup_status = not_exposed_by_web_igv
  kalır.
- Read probe, tam pileup değildir; yalnız örneklenmiş web popup verisidir.

ÖNEMLİ: SOURCE_CALL
-------------------
SNV dışa aktarımından daha önce toplanmış AF/AD/DP/filter bilgisi `source_call` olarak YZ paketine girer.
Web IGV bunun yerine sayı uydurmaz; source_call ile web track/popup bulgularını yan yana verir.

DOSYALAR
--------
Yeni tek-YZ ZIP içinde:
- IGV_WEB_EVIDENCE.json
- IGV_EVIDENCE.json (geri uyumlu aynı içerik alias'ı)
- SOURCE_AI_REVIEW_RETURN.json
- AI_IGV_RETURN_TEMPLATE.json
- PROMPT.md
- WEB_IGV_VIEW.html
- MANIFEST.json
- yalnız kullanıcı web_igv_capture_screenshot=true yaparsa IMAGES/

ARAYÜZ
------
Eski butonlar web-veri odaklı adlandırılır:
- Web IGV Verisini Al
- Seçili Web IGV Verisini Topla
- Tüm Web IGV Verisini Topla
- Web IGV Verisi ZIP
- Web IGV Veri Görünümü

v0.4.33 ile eklenen BAM/CRAM + BED seçim paneli varsa gizlenir.
`WEB-DATA` durumu, ekran görüntüsü olmasa da yapılandırılmış web IGV verisinin hazır olduğunu gösterir.

v0.4.33 UYUMLULUĞU
-------------------
- Eski snapshot audit/request lineage verileri silinmez.
- Eski screenshot kayıtları arşivde kalabilir.
- v0.4.34'ten itibaren yeni IGV işi screenshot gerektirmez.
- pileup_engine.py varsa yerel dosya çözümleme fonksiyonları safe-disabled hale getirilir.
- requirements.txt içindeki pysam satırı kaldırılır; artık gerekli değildir.
- config.local.json varsa önce yedeklenir ve BAM/BED alanları boşaltılır.

KURULUM
-------
1. NGS Mutasyon Havuzu'nu tamamen kapatın.
2. ZIP'i ngs_toplu_collector klasörünün BİR ÜSTÜNE çıkarın.
3. UYGULA_HOTFIX_v0.4.34.bat çalıştırın.
4. Programı yeniden açın.
5. YZ Dönüş / IGV bölümünde "Tüm Web IGV Verisini Topla" ile deneyin.

TEST
----
py TEST_HOTFIX_v0.4.34.py

NOT
---
Web GenNext/IGV sürümü değişirse popup biçimi değişebilir. Ham popup metni her zaman korunur; parser bilinmeyen
alanı klinik sayıya çevirmek yerine boş bırakır. Bu özellikle yanlış DP/AD üretmemek için kasıtlıdır.
