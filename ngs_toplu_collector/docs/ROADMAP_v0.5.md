# NGS Mutasyon Havuzu — v0.5 geliştirme yol haritası
**Tarih:** 28 Ekim 2026
**Çalışan yerel referans:** v0.4.92 (kullanıcı bildirimi; canlı sistemin tüm yolları test edilmiş değildir)
**Geliştirme yaklaşımı:** v0.4.92 korunsun; v0.5 ayrı dal/çekirdekte aşamalı geliştirilsin.

## Değişmez klinik kurallar
- Teknik kimlik = `runUuid::sampleUuid::sampleType`; görünen MP adı, sıra veya sayfalama tek başına yeterli değil.
- GenNext Analyzed, DNA/RNA Selection, YZ önerisi, IGV sonucu ve patologun yerel rapor kararı ayrı kaydedilir.
- RNA `Analyzed=on, Selection=0` tek başına **negatif** değildir. Negatif ancak doğrulanmış rapor kararı ile gösterilir.
- İptal, yetersiz, inceleme tamamlandı fakat rapor verilmedi ve yeniden açıldı durumları negatiflikten farklıdır. Manuel kapatma elektronik imzayı kanıtlamaz.
- Belirsiz onkojenisite sessizce düşürülmez. YZ önerisi rapora kendiliğinden yazılmaz.
- Yanlış UUID veya HTTP 404/500 nedeniyle kaynak okunamazsa sonuç otomatik negatif/raporlandı sayılmaz; eski kanıtlar silinmez.

## P0 — Önce güvenilir veri toplama
1. GenNext'in resmi API/export desteğini ve kullanım iznini doğrula; desteklenen kimliklerle uçları haritala.
2. Kaynağı salt okunur API istemcisi ile oku; yanıtın run/sample/type ve klinik şema bütünlüğünü doğrula; onaysız endpointlerde DOM fallback.
3. Eski URL tahminini ve başka hastadan "peer link" denemesini kaldır; 404/500, auth, rate-limit ve SPA yükleme hatalarını ayrı sınıflandır.
4. Küçük eşzamanlılık, üstel aralıklı yeniden deneme, 429 Retry-After, iptal ve kalıcı eksik iş kuyruğu; 404 için kontrollü geri keşif.
5. Raporlanan seçimler kaybolmadan DNA Clinical SNV/indel, CNV, TMB, MSI, HRD; RNA Fusion Genes / Selection / QC doğrula.
6. Otomatik tarayıcının açtığı sekmeleri iş sonunda kapat; ana RUNS ve kullanıcı sekmeleri kalsın; hata/timeout da temizlensin.
7. Tanı paketi hasta bilgisi, gerçek UUID, URL query, token/cookie, ağ gövdesi ve rapor cümlesi taşımamalı.

## P1 — Rapor arşivi ve audit
- Teknik örnek + hasta bağlantısı + moleküler bulgu + kaynak/kapsam/kanıt/provenans için normalize veri şeması; idempotent göç ve geriye uyumluluk.
- Analyzed, Selection, klinik rapor sonucu, Enlil taslağı ve elektronik imza birbirinden bağımsız.
- Manuel: pozitif, negatif, iptal, yetersiz, raporsuz kapatıldı, yeniden aç; aktör/tarih/gerekçe/geçmiş değişmez.
- DNA/RNA ayrı teknik kayıtlar, tek hasta arşivinde bağlantılı; CNV ve füzyonlar da aranabilir.
- Rapor cümlesi özgün metin halinde korunur; normalleştirme rapor içeriğini değiştirmez.

## P2 — YZ ve teknik değerlendirme
- YZ girdi şemasını ham veriden değil doğrulanmış kanıt ve gerekçeli aday listelerinden üret.
- DNA teknik QC, VAF, DP/AD, LoD, IGV, CHIP/germline olasılığı, havuz-içi yayılım yalnız uyarı/ön eleme; eşik ve tümör bağlamı doğrulansın.
- RNA junction, frame, breakpoint, read support, füzyon yönelimi ve artefakt incelemesi ayrı kanıt.
- Klinik sınıflandırmalar ve dış kaynak annotasyonları sürümlü/tarihli; nihai patolog kararı korunur.
- Eski AI_RETURN/IGV JSON sözleşmelerini bozmadan geçiş.

## P3 — Kullanım/operasyon
- Tkinter arayüzünü hemen değiştirme; normalize view model ve adapter kurulduktan sonra yerel web UI prototipi değerlendir.
- "Son POOL tamamlandı" yalnız kaynak indirmeyi ifade ediyorsa Selection ve rapor durumları ayrı açıkça gösterilsin.
- Test senaryoları: MP etiket kayması, bozuk 404, geç 200, API 500, auth redirect, React tab değişimi, boş Selection, olumlu füzyon, manuel negatif, iptal/yetersiz, retry, crash recovery, açık sekme.
- Her sürüm: geri alma betiği, Python sözdizim/regresyon, gerçek Windows oturumunda kullanıcı kabul testi ve otomatik privacy taraması.

## v0.4.92 kabul kaydı / bilinen açık konular
- 28 Ekim 2026 tarihinde kullanıcı v0.4.92 için "iyi gibi" dedi.
- Aynı gün RUNS taraması ve Son POOL işlemi tanı özetleri `completed`, `worker_error_class=null`; yine de 5 ve 14 HTTP 500 ağ yanıtı kaydetti.
- Bu kayıtlar bütün klinik kaynakların tamamlandığını kanıtlamaz. API verisi uçtan uca henüz yeniden yazılmamıştır.
- Geçmiş `self_test.py` teknik örnek kimliği tutarsızlığı ayrıca ele alınmalıdır.

## Gizlilik
Bu repo herkese açık. Gerçek hasta adı, TC, örnek/RUN UUID'leri, rapor dosyaları, SQLite, PROFILE, oturum çerezleri, kimlik bilgileri, DIAGNOSTICS, API yanıt gövdeleri veya klinik paketler commit edilmez.
