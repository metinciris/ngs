# NGS QC Raporlayıcı

**NGS QC Raporlayıcı**, DNA ve RNA dizileme kalite kontrol dosyalarını yerel bilgisayarda inceleyen ve patoloji raporunda kullanılabilecek standart Türkçe metinler oluşturan Tkinter tabanlı bir masaüstü uygulamasıdır.

Uygulama Altium/Zebra FASTQ istatistik raporlarını ve Cutadapt trimming çıktılarını aynı örnek altında birleştirir. Protokol numarası ile DNA/RNA türünü dosya adından otomatik tanır.

```text
99D_L01_227.report.html  → Protokol 99 / DNA
99R_L01_234.report.html  → Protokol 99 / RNA
```

## Özellikler

- DNA ve RNA örneklerini otomatik ayırır.
- Aynı örneğe ait HTML, `fqStat.txt` ve `.trimming.report` dosyalarını birleştirir.
- Paired-end okuma çifti, Q30, GC, temiz veri ve baz korunma oranını çıkarır.
- DNA ve RNA için kopyalanabilir QC cümlesi üretir.
- Genel Kanser Paneli ve Genel Füzyon Paneli standart metinlerini oluşturur.
- TMB, MSI Percentage ve HRD Score alanlarını rapor cümlesine dönüştürür.
- Mutasyon veya füzyon işaretlenen hedefe ait negatif sonucu otomatik kaldırır.
- Pozitif bulgunun açıklamasını kullanıcıya bırakır; boş gen başlığı üretmez.
- SDÜ Tıp Fakültesi veya DIŞ parafin blok kaynağını destekler.
- Dört doktorlu imza bloğunu raporun en altına ekler.
- Seçilen imza sahibini sağ alt köşeye yerleştirir.
- Doktor isimlerini ve imza seçimini yerel ayar dosyasında saklar.
- ZIP arşivlerini doğrudan, RAR arşivlerini WinRAR/UnRAR/7-Zip yardımıyla açabilir.
- Verileri internete göndermez; analiz yerel bilgisayarda yapılır.
- Yalnızca Python standart kütüphanesini kullanır.

## Çalıştırma

Windows'ta aşağıdaki dosyaya çift tıklayın:

```text
ngs_qc_raporlayici_v1_9.pyw
```

Komut satırından:

```bash
python ngs_qc_raporlayici_v1_9.py
```

Pencere başlığında `NGS QC Raporlayıcı 1.9` yazmalıdır.

## Kullanım

1. **Klasör Seç** ile QC dosyalarının bulunduğu klasörü açın.
2. Sol listeden protokolün DNA veya RNA satırını seçin.
3. **Standart Metinler** sekmesine geçin.
4. Blok kaynağını, blok numarasını ve tümör oranını girin.
5. DNA için TMB, MSI Percentage ve HRD Score değerlerini girin.
6. Pozitif mutasyon veya füzyon varsa ilgili hedefi işaretleyin. Uygulama yalnızca negatif cümleyi kaldırır; pozitif bulgu metni kullanıcı tarafından yazılır.
7. İmza sahibini seçin. Seçilen doktor sağ altta yer alır.
8. **Tüm Standart Metni Kopyala** düğmesine basın.
9. Metni Notepad veya raporlama sistemine yapıştırın.
10. `-------------` çizgisinin üzerindeki teknik bölümü klinik rapora aktarmayın.

## Doktor isimlerinin kaydedilmesi

Doktor isimleri arayüzde bir kez düzenlenip **Doktor isimlerini kaydet** düğmesine basılarak saklanabilir. Uygulama kapanırken güncel isimler ve imza seçimi ayrıca sessizce kaydedilir.

Windows kayıt yeri:

```text
%APPDATA%\NGS_QC_Raporlayici\settings.json
```

Linux kayıt yeri:

```text
~/.config/NGS_QC_Raporlayici/settings.json
```

Bu dosya proje veya GitHub deposunun içine yazılmaz. Hasta/protokol bilgileri ayar dosyasına kaydedilmez.

## İmza düzeni

Dört doktor iki satır halinde yerleştirilir. İmza sahibi olarak seçilen doktor her zaman ikinci satırın sağında bulunur. Diğer üç doktorun yerleşimi kurumsal düzene göre otomatik değiştirilir.

## Desteklenen dosyalar

```text
.html
.htm
.txt
.log
.report
.csv
.tsv
.zip
.rar
```

Örnek Altium dosyaları:

```text
99D_L01_227.report.html
99D_L01_227.trimming.report
99D_L01_227_1.fq.fqStat.txt
99D_L01_227_2.fq.fqStat.txt
```

Ham dizileme ve varyant dosyaları analiz edilmez:

```text
.fastq
.fq
.fastq.gz
.fq.gz
.bam
.cram
.vcf
```

## Otomatik sınıflama

```text
MSI-stable: <%15
MSI-Low:    %15–40
MSI-High:   >%40

TMB-L:      <10 mut/Mb
TMB-H:      ≥10 mut/Mb
```

Bu sınırlar uygulamada sabittir ve laboratuvarın valide edilmiş raporlama yaklaşımına göre tanımlanmıştır.

## Sınırlamalar

- Uygulama FASTQ/trimming kalite özetini raporlar; mapping, on-target oranı, unique depth, coverage uniformity ve biyoinformatik kontrol sonuçlarının yerine geçmez.
- Üretilen metin kullanıcı tarafından kontrol edilmeden klinik rapora aktarılmamalıdır.
- Pozitif varyant ve füzyon açıklamaları uygulama tarafından oluşturulmaz.
- RAR desteği için bilgisayarda WinRAR, UnRAR veya 7-Zip bulunmalıdır.

## Gizlilik

Analiz edilen QC dosyaları harici bir sunucuya gönderilmez. İşlem yerel bilgisayarda gerçekleşir. GitHub deposuna gerçek hasta, protokol, varyant veya QC dosyaları eklenmemelidir.

## Sürüm

### v1.9

- Yerel olarak kaydedilen doktor isimleri ve imza sahibi seçimi eklendi.
- İmza sahibinin sağ altta olduğu dört kurumsal dizilim eklendi.
- Pozitif hedef seçimlerinde boş mutasyon/füzyon başlıkları kaldırıldı.
- DNA yöntem adı `Genel Kanser Paneli` olarak düzenlendi.
- Dış blok kaynağı `DIŞ`, HRD sonucu `HRD skoru` biçiminde standardize edildi.
