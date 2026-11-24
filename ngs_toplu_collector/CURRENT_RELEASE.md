# NGS Mutasyon Havuzu — Kaynak sürüm kaydı (24.11.2026)

**Yerelde doğrulanan çalışan hat:** NGS Mutasyon Havuzu **v0.4.95** · Bilgi Atlası **v1.6** · Boyama **GenNext Web + MultiQC v1.8.0**.

## Kaynakların kökeni ve doğrulama

Bu sürümde ana dizindeki **90 dosya**, 24.11.2026 13:59 tarihli kullanıcının **doğrudan çalışan Windows kurulumundan** oluşturduğu `NGS_KAYNAK_YALNIZ_KOD_20261124_135912.zip` paketinden alındı. Her dosyanın SHA-256 değeri, aynı tarihli kullanıcı kontrol raporundaki değerle karşılaştırıldı ve eşleşti. Python dosyaları sözdizimi açısından derlendi.

**BİLİNEN EKSİKLİK — Tam kurulabilir sürüm değildir:** Dışa aktarıcı, içerikteki hassas olabilecek sabit metinleri gördüğü için **37 ek Python dosyasını pakete hiç koymadı**. Bunlar arasında `collector.py`, `qc_enlil.py` ve `ngs_boyama_web_hybrid_v180.py` gibi programın çalışması için kritik dosyalar vardır. Bu dosyalar bu kaynak güncellemesinde *kullanıcının canlı sürümü olarak yayımlanmadı*. Bu depo ve buradaki kaynak alt kümesi, Windows'daki çalışan klasörün eksiksiz kopyası olarak yorumlanmamalıdır.

## Boyama QC'nin yerelde doğrulanan akışı

- GenNext RUN Detail / **QC Summary**: aynı RUN + DNA + örnek kimliğine bağlı ortalama/medyan kapsama, kapsama eşikleri ve ortalama okuma uzunluğu.
- **MultiQC**: web sayfasında bulunmayan, doğrulanmış GC, temiz veri, duplikasyon ve VerifyBAMID FREEMIX verilerini aynı teknik kayıtla eşleştiğinde tamamlama.
- Çelişen farklı analiz kapsamları, yanlış RUN veya farklı hastanın QC verisi rapora otomatik yazılmaz. Otomatik üretilen teknik değerler klinik rapor onayı değildir.
- Kullanıcı, bu düzenin MP173-26, MP268-26, MP288-26 için Boyama paragrafı oluşturduğunu bildirdi. Bu kaynak arşivinin başka bilgisayarda kurulup çalıştırıldığı test edilmemiştir.

## Güvenlik ve eksik dosyalar

`DATA`, `OUTPUT`, `PROFILE`, `AI_EXPORT`, `IGV_SNAPSHOTS`, QC ZIP'leri, veritabanları, vaka veri dosyaları, raporlar, parolalar, profil/oturum dosyaları ve kullanıcının dışa aktarma kontrol raporu **GitHub'a alınmadı**. Dışa aktarma filtresi 37 hassas olabilecek kaynak dosyasını da dışarıda bıraktı; bunlar için ayrı bir kod/gizlilik incelemesi gerekir.

**Korunan kural:** Çalışan Windows sürümü, yalnızca GitHub'daki bu kaynak alt kümesinden yeniden kurulmaya çalışılarak üzerine yazılmamalıdır.
