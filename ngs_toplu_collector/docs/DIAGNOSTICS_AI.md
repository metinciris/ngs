# Diagnostics / AI bakım paketi - v0.1.5

Browser otomasyonu bir adımda başarısız olursa DIAGNOSTICS altında kendi klasörünü üretir.

Önemli dosyalar: `context.json`, `page.html`, `screenshot.png`, `visible_text.txt`, `frames.json`, `frame_*.html`, `controls.json`, `events.log`.

`controls.json` artık button, a, input yanında `span.btn` ve `div.btn` de içerir. Bu NGS analiz platformu'teki `Download as TSV` gibi button görünümünde fakat gerçek tag'i `span` olan kontrolleri yakalamak için gereklidir.

YZ bakımında önce exact id/name/title/href/class-prefix selectorları tercih edilmeli; yalnız metne dayalı geniş fallback son çare olmalıdır.


### Zamanlama tanısı
`visible_text.txt` içinde `Loading QC report…` bulunması artık selector hatası değil, readiness bekleme sinyalidir. CNV için de control görünür olsa bile aktif loader/stabil olmayan DOM varsa indirme ertelenir.

## Normal durumda klasörün boş olması

`DIAGNOSTICS/` yalnız hata tanısı içindir. Başarılı rutin toplamada klasörün boş kalması beklenir ve UI'da `DIAGNOSTICS: boş ✓` olarak gösterilir.
