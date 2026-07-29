# NGS QC Raporlayıcı

**NGS QC Raporlayıcı**, Altium tarafından üretilen DNA/RNA dizileme kalite kontrol dosyalarını yerel bilgisayarda analiz eden ve patoloji raporunda kullanılabilecek standart Türkçe metinler oluşturan, Tkinter tabanlı bir masaüstü uygulamasıdır.

Uygulama; FASTQ istatistik raporlarını, Zebra HTML raporlarını ve Cutadapt trimming çıktılarını aynı örnek altında birleştirir. Protokol numarasını ve çalışma türünü dosya adından otomatik tanır.

> Örnek dosya adı: `99D_L01_227.report.html`  
> Protokol: `99` · Çalışma türü: `DNA` · Çalışma/dosya kodu: `227`

## Temel özellikler

- DNA ve RNA örneklerini otomatik ayırır.
- Protokol numarasını dosya adından tanır.
- Aynı örneğe ait HTML, `fqStat.txt` ve `.trimming.report` dosyalarını birleştirir.
- Toplam paired-end okuma çifti, Q30, GC, temiz veri ve baz korunma oranını çıkarır.
- DNA ve RNA için kopyalanabilir QC cümlesi üretir.
- DNA Tümör Paneli ve Genel Füzyon Paneli için standart rapor metni oluşturur.
- Tümör oranı, TMB, MSI Percentage ve HRD Score alanlarını destekler.
- TMB ve MSI sınıflamasını ayarlanabilir eşiklerle otomatik yapar.
- Mutasyon veya füzyon bulunan genleri negatif sonuç listesinden çıkarır.
- Pozitif/işaretli genleri sonuç bloğunun üstünde boş açıklama alanı olarak bırakır.
- Teknik dosya bilgilerini rapora girmeyecek ayrı bir başlıkta gösterir.
- Oluşturulan tam metni tek tuşla panoya kopyalar.
- QC sonuçlarını CSV olarak dışa aktarabilir.
- ZIP arşivlerini doğrudan okuyabilir.
- RAR arşivlerini, sistemde WinRAR/UnRAR veya 7-Zip varsa açabilir.
- Yalnızca Python standart kütüphanesini kullanır; ek Python paketi gerektirmez.
- Verileri internet ortamına göndermez; analiz tamamen yerel bilgisayarda yapılır.

## Ekran ve iş akışı

1. **Klasör Seç** ile QC dosyalarının bulunduğu klasörü açın.
2. Sol listedeki protokol ve DNA/RNA satırını seçin.
3. QC cümlesini kontrol edin.
4. **Standart Metinler** sekmesine geçin.
5. Tümör oranı ve gerekli DNA metriklerini girin.
6. Mutasyon veya füzyon bulunan genleri işaretleyin.
7. **Tüm Standart Metni Kopyala** düğmesine basın.
8. Metni Notepad veya raporlama sistemine yapıştırın.
9. `-------------` çizgisinin üzerindeki teknik alanı klinik rapora aktarmayın.

## Gereksinimler

- Windows 10 veya Windows 11
- Python 3.10 veya daha yeni bir sürüm
- Python kurulurken **Tcl/Tk and IDLE** bileşeninin etkin olması

RAR dosyalarının doğrudan açılması için aşağıdakilerden biri gereklidir:

- 7-Zip
- WinRAR
- UnRAR

RAR desteği bulunmazsa arşivi elle çıkartıp klasörü seçebilirsiniz.

## Çalıştırma

Depoyu indirin veya ZIP olarak çıkartın. Ardından aşağıdaki dosyaya çift tıklayın:

```text
ngs_qc_raporlayici_v1_5.pyw
```

Komut satırından çalıştırmak için:

```bash
python ngs_qc_raporlayici_v1_5.py
```

Pencere başlığında aşağıdaki sürüm bilgisi görünmelidir:

```text
NGS QC Raporlayıcı 1.5
```

## Desteklenen dosyalar

Uygulama aşağıdaki dosya ve arşiv türlerini tarar:

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

Özellikle aşağıdaki Altium çıktılarını tanır:

```text
99D_L01_227.report.html
99D_L01_227.trimming.report
99D_L01_227_1.fq.fqStat.txt
99D_L01_227_2.fq.fqStat.txt
```

Dosya adı şablonu:

```text
<protokol><D veya R>_L01_<çalışma kodu>...
```

- `D`: DNA
- `R`: RNA

Uygulama büyük ham veri dosyalarını açmaz:

```text
.fastq
.fq
.fastq.gz
.fq.gz
.bam
.cram
.vcf
```

## Hesaplanan QC alanları

### DNA

- Paired-end okuma çifti sayısı
- Toplam bireysel okuma sayısı
- Q30 baz oranı
- GC oranı
- Trimming sonrası temiz veri miktarı
- Baz korunma oranı
- Okuma geçiş oranı

Örnek çıktı:

```text
DNA dizilemesinde 57,35 milyon paired-end okuma çifti elde edilmiştir. Q30 baz oranı %98,08, GC oranı %46,63 ve trimming sonrası temiz veri miktarı 16,24 Gb olarak bulunmuştur. Baz korunma oranı %94,40 olarak bulunmuştur.
```

### RNA

- Paired-end okuma çifti sayısı
- Toplam bireysel okuma sayısı
- Q30 baz oranı
- Trimming sonrası temiz veri miktarı
- Baz korunma oranı
- Okuma geçiş oranı

Örnek çıktı:

```text
RNA dizilemesinde 17,15 milyon paired-end okuma çifti elde edilmiştir. Q30 baz oranı %98,60 ve trimming sonrası temiz veri miktarı 4,41 Gb olarak bulunmuştur. Baz korunma oranı %85,81 olarak bulunmuştur.
```

## Standart rapor metni

Tam metnin başında rapora aktarılmaması gereken teknik bilgiler bulunur:

```text
[RAPORA GİRMEYECEK TEKNİK BİLGİ]
Protokol: 99
Çalışma türü: DNA
Örnek kodu: 99D
Çalışma/dosya kodu: 227
QC durumu: Tam
Kaynak QC dosya sayısı: 4
Kaynak QC dosyaları: ...
-------------
```

Ayraçtan sonra raporda kullanılabilecek standart metin başlar:

```text
SDÜ Tıp Fakültesi parafin bloktan çalışılmıştır. Blokta tümör oranı %...

Tedavi planlaması için NGS (yeni nesil dizileme) yöntemi ile 'DNA Tümör Paneli' çalışması yapılmıştır.
```

## TMB, MSI ve HRD

DNA seçildiğinde aşağıdaki alanlar kullanılabilir:

- **TMB:** mut/Mb
- **MSI Percentage:** yüzde
- **HRD Score:** sayısal skor

Varsayılan TMB sınıflaması:

```text
TMB < 10 mut/Mb  → TMB-L
TMB ≥ 10 mut/Mb  → TMB-H
```

Varsayılan MSI sınıflaması:

```text
MSI Percentage = %0     → Mikrosatellit Stabil (MSS)
MSI Percentage > %0–40  → MSI-Low
MSI Percentage > %40    → MSI-High
```

Bu eşikler uygulama arayüzünden değiştirilebilir. Kullanılacak eşikler, laboratuvarın valide edilmiş analiz algoritması ve raporlama politikasına göre belirlenmelidir.

## Mutasyon ve füzyon seçimi

### DNA

BRAF, EGFR, KRAS, NRAS veya TERT işaretlenirse gen, pozitif bulgunun manuel olarak yazılabilmesi için sonuç bloğunun üstünde bırakılır:

```text
- BRAF:
```

Aynı gene ait negatif sonuç cümlesi otomatik olarak kaldırılır. İşaretlenmeyen genler için negatif sonuç cümleleri korunur.

### RNA

RNA panelindeki bir gen işaretlendiğinde:

- Gen adı sonuç bloğunun üstünde boş alan olarak gösterilir.
- Gen, genel füzyon-negatif listesinden çıkarılır.
- NTRK, FGFR, ROS1 ve ALK grup/özel cümleleri seçime göre yeniden düzenlenir.

## CSV dışa aktarma

Uygulama analiz edilen örnekleri CSV olarak kaydedebilir. Dışa aktarılan alanlar, sürüme göre değişebilmekle birlikte temel olarak şunları içerir:

- Örnek kodu
- Protokol
- DNA/RNA türü
- Okuma çifti
- Q30
- GC
- Temiz veri
- Baz korunma oranı
- Okuma geçiş oranı
- Kaynak dosyalar
- Üretilen QC cümlesi

## Dosya yapısı

```text
NGS-QC-Raporlayici/
├── ngs_qc_raporlayici_v1_5.py
├── ngs_qc_raporlayici_v1_5.pyw
└── README.md
```

- `.py`: Komut satırından çalıştırılabilen kaynak dosyası
- `.pyw`: Windows'ta konsol penceresi açmadan çift tıklanabilen sürüm

## Sınırlamalar

Bu uygulama:

- Ham FASTQ sekanslarını analiz etmez.
- Alignment gerçekleştirmez.
- BAM/CRAM veya VCF yorumlamaz.
- Hedef kapsama derinliğini hesaplamaz.
- On-target oranı, duplicate oranı veya coverage uniformity değerlendirmez.
- CNV, SNV/indel veya füzyon çağrısı yapmaz.
- Klinik varyant sınıflaması yapmaz.
- TMB, MSI veya HRD değerini biyoinformatik olarak hesaplamaz; yalnızca girilen sonucu biçimlendirir.

Uygulama, mevcut FASTQ istatistik ve trimming raporlarından teknik bir özet üretir. Klinik raporlanabilirlik kararı; post-alignment kalite metrikleri, valide edilmiş eşikler, kontroller ve uzman değerlendirmesiyle verilmelidir.

## Gizlilik

- Analiz yerel bilgisayarda yapılır.
- Dosyalar herhangi bir sunucuya gönderilmez.
- Gerçek hasta adları, kimlik bilgileri, protokol numaraları veya QC dosyaları GitHub deposuna yüklenmemelidir.
- Örnek çıktı paylaşılacaksa bilgiler anonimleştirilmelidir.

## Doğrulama

v1.5 sürümü, aynı koşuya ait 60 QC dosyası ve 15 DNA/RNA kütüphanesinden oluşan test klasöründe doğrulanmıştır:

```text
60 dosya incelendi
15 örnek bulundu
15 örnek için tam QC cümlesi üretildi
```

Bu doğrulama, kullanılan örnek dosya biçimleri için parser işlevini göstermektedir; klinik analitik validasyon yerine geçmez.

## Sürüm geçmişi

### v1.5

- `Solid Tümör Paneli` ifadesi `DNA Tümör Paneli` olarak değiştirildi.
- Standart metinler tek blok halinde düzenlendi.
- Tek tek kopyalama alanları kaldırıldı.
- Rapora girmeyecek teknik bilgi bloğu eklendi.
- Protokol, çalışma türü, örnek kodu, çalışma kodu ve kaynak dosyalar gösterilmeye başlandı.

### v1.4

- DNA/RNA negatif sonuçları tek blok halinde birleştirildi.
- Mutasyon veya füzyon bulunan genlerin negatif listeden çıkarılması eklendi.
- Pozitif genler için boş manuel açıklama alanı oluşturuldu.

### v1.3

- DNA Tümör Paneli ve Genel Füzyon Paneli standart metinleri eklendi.
- TMB, MSI Percentage ve HRD Score alanları eklendi.
- Otomatik TMB/MSI sınıflaması eklendi.

### v1.2

- Protokol ve DNA/RNA türünün dosya adından güvenilir biçimde çıkarılması sağlandı.
- Altium `fqStat` ve Zebra HTML biçimleri için parser geliştirildi.

### v1.1

- `.trimming.report`, `#ReadNum`, `#Q30%`, `#GC%` ve `TotalReads(M)` biçimleri desteklendi.

## Uyarı

Bu yazılım klinik iş akışını kolaylaştırmak amacıyla geliştirilmiş yardımcı bir araçtır. Üretilen tüm metinler, hasta raporuna aktarılmadan önce sorumlu patolog veya moleküler patoloji uzmanı tarafından kontrol edilmelidir.
