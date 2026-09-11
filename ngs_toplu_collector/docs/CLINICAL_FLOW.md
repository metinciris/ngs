# Clinical Flow - Playwright v2

Clinical artık tarayıcı eklentisine bağlı değildir.

URL:
`/somatic-detail-clinic/<runUuid>&<sampleUuid>&snv`

Akış:
1. Gen accordion listesi render olana kadar bekle.
2. `Expand All` varsa tıkla.
3. Hâlâ kapalı accordion'ları kontrollü aç.
4. `.accordion-item.clinic` içindeki `.slick-row` varyant satırlarını ve hücre metinlerini al.
5. Pagination bilgisini kaydet.
6. Sonraki sayfa için yalnız `i.fa-solid.fa-forward-step` ikonunun bağlı olduğu butonu kullan.
7. Gen imzasının değiştiğini doğrula.
8. Toplam gen sayısı biliniyorsa yakalanan benzersiz gen sayısı ile doğrula.
9. Tüm veri tek CLINICAL JSON'a yazılır.

Güvenlik: aynı gen grubu ikinci kez görünürse veya Next sonrası sayfa değişmezse sessizce devam edilmez; DIAGNOSTICS üretilir.
