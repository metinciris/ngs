# Hasta / Blok Metadata

## Vaka düzeyi
Aynı vaka ailesindeki rerunlar paylaşır:
- Ad Soyad
- TC Kimlik No
- **Tanı anı yaşı**
- Tümör tanısı
- Cinsiyet
- İstem doktoru
- Raporlama doktoru
- Vaka notu

Doktor alanları editable Combobox'tır. Daha önce girilmiş doktor adları yazdıkça önerilir; yeni bir isim serbestçe yazılabilir.

## Teknik örnek düzeyi
Her run/teknik örnek için ayrıdır:
- Blok
- Tümör hücre oranı (%)
- Teknik örnek notu

`Çalışılan blok bölümü` v0.2.3 itibarıyla arayüzden kaldırılmıştır. Eski DB sütunu veri kaybını önlemek için tutulur ancak kullanılmaz.

`Tanı anı yaşı` sabit bir klinik değerdir; güncel yaş gibi yıllar içinde otomatik değişmez.
