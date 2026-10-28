# NGS Mutasyon Havuzu — çalışan yerel referans

**Yerel referans:** v0.4.92 (28 Ekim 2026; kullanıcı geri bildirimi: "iyi gibi")  
**GitHub tam kaynak eşitleme durumu:** **HENÜZ TAMAMLANMADI** — depodaki Python kaynakları bu sürümün birebir eşleniği olarak kabul edilmemeli.  
**GitHub tarihsel kaynak anlık görüntüsü:** v0.4.83  
**Kurulum konumu:** `C:\ngs\ngs_toplu_collector`

## v0.4.92 yerel değişiklik çizgisi
- v0.4.85–90: GenNext oturum/404 denetimi, API gözlemi, RNA Fusion Selection sekme kontrolü ve arşiv görünürlüğü.
- v0.4.91: teknik örnek düzeyinde manuel rapor kapanışı (pozitif, negatif, iptal, yetersiz, raporsuz kapatıldı, yeniden aç) ve denetim geçmişi.
- v0.4.92: yalnız tarayıcının otomatik açtığı RNA sekmelerini toplama sonunda/hata halinde kapatma; manuel ve ana RUNS sekmeleri koruma.

## Son kontrol (28 Ekim 2026)
Kullanıcının iki tanı ZIP özetinde `scan_runs` ve `complete_latest` işçi sonucu `completed`, Python istisnası yoktur. Ağ trafiğinde sırasıyla 5 ve 14 HTTP 500 olayı sürmüştür. Bu nedenle her teknik kaynağın klinik olarak tamamlandığı iddia edilmez. Çevrimdışı hedefli testler geçti; canlı GenNext uçtan uca doğrulama ve geçmişte bildirilen genel teknik kimlik testi sorunu açık kalmıştır.

## Raporun kesinleşmesi
`GenNext Analyzed`, `GenNext Selection`, `YZ önerisi` ve `yerel patolog rapor kararı` birbirinden bağımsızdır. `Selection=0`, tek başına negatif değildir. Kullanıcı tarafından rapor sonucu doğrulanınca ayrı kaydedilir. Manuel negatif kapatma oturumun elektronik imzalandığını kanıtlamaz.

## Sonraki adım
[v0.5 Yol Haritası](docs/ROADMAP_v0.5.md): API öncelikli doğrulanabilir veri toplama, UUID koruması, kapsamlı DNA/RNA rapor arşivi ve yeniden denenebilir eksik iş kuyruğu.

## Kaynak eşitlemesi için yayın koşulu
Gerçek v0.4.92 kaynak arşivinin gizlilik incelemesi, dosya bütünlüğü, test ve ana dalla fark karşılaştırması tamamlanmadan depodaki `VERSION` veya eski Python kodları 0.4.92 olarak etiketlenmez. Hasta/örnek bilgileri, çalışma veritabanı, gerçek UUID, PROFILE, DIAGNOSTICS, özel ayarlar, tokenlar veya raporlar GitHub'a yüklenmez.
