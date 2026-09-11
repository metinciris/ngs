# Tüm Arşiv / Mutasyon Arama

Oncogenic ve Likely Oncogenic SNV indeksi üzerinden bütün POOL'larda arama yapılır.

Arama örnekleri:
- `KRAS`
- `G12C`
- `TP53`
- protein değişikliği
- genomik varyant anahtarı
- örnek/MP etiketi

Bir mutasyon seçildiğinde tüm gözlemler hasta odaklı gösterilir:
- Ad Soyad
- Örnek/MP
- TC
- POOL
- Tümör tanısı
- **Tanı anı yaşı**
- Blok
- Tümör hücre oranı %
- AF / AD / DP
- Filter
- Run depth
- Oncogenic sınıfı / Level
- Rerun

Sample UUID analiz anahtarı olarak arka planda korunur ancak günlük arayüzde öncelikli gösterilmez.
