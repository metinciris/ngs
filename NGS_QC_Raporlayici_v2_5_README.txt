NGS QC Raporlayıcı v2.5
========================

Bu sürüm, v2.4 protokol eşleştirmeli hibrit QC yapısını korur.

Yenilikler
----------
1. RNA rapor paragrafından VerifyBAMID FREEMIX çıkarıldı.
   - RNA FREEMIX değeri ham teknik veri/CSV içinde korunur.
   - Klinik veya kopyalanabilir RNA QC paragrafına yazılmaz.

2. DNA FREEMIX için yalnız teknik inceleme uyarısı eklendi.
   - >= %2: "FREEMIX artmış - gözden geçir"
   - >= %10: "FREEMIX yüksek - inceleme gerekli"
   - Bu eşikler otomatik kabul/ret kriteri değildir.

3. Kapsama cümlesi sadeleştirildi.
   - Eski: "en az 100x kapsanan hedef bölge oranı..."
   - Yeni: "Hedef bölgelerin %...’i en az 100x kapsanmıştır."
   - RNA için "RNA panel hedeflerinin..." ifadesi kullanılır.

4. Büyük kapsama değerleri Türkçe binlik ayırıcıyla gösterilir.
   - Örnek: 11460,20x yerine 11.460,20x

5. Post-alignment MultiQC bulunmayan örneklerde teknik bilgi alanına
   temel QC metriklerinin kullanıldığına dair not eklenir.

Kullanım
--------
- ngs_qc_raporlayici_v2_5.pyw dosyasına çift tıklayın.
- İlk nesil HTML/TXT/trimming dosyaları ve MultiQC ZIP paketlerinin
  bulunduğu klasörü seçin.
- Program DNA ve RNA protokollerini hasta bazında eşleştirir.

Not
---
DNA FREEMIX uyarıları yalnız kalite gözden geçirmesini kolaylaştırır;
sonuçların klinik ve teknik bağlamda değerlendirilmesi gerekir.
