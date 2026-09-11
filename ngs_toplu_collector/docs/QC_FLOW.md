# QC flow

Normal akış: Runs sample satırı -> QC Report -> MultiQC -> Export > Data -> Download Plot Data -> uyarı popup kabulü -> ZIP/TSV kaydı.

## v0.2.4 hazır-değil durumları

Aşağıdakiler collector hatası değildir ve `waiting` olarak kaydedilir:

- parent RUN veya sample henüz `COMPLETED` değil;
- QC child sayfası birkaç saniye sonra hâlâ `about:blank`;
- sayfada veya dialog içinde `The QC report could not be loaded.` mesajı;
- `Loading QC report…` metni QC DOM zaman aşımına kadar sürüyor.

Bu durumlarda DIAGNOSTICS paketi üretilmez. Bir sonraki RUNS taraması/eksikleri tamamlama denemesinde QC tekrar kontrol edilir.

RUN `COMPLETED` olduğu halde bunların dışında beklenmedik DOM/selector/download hatası oluşursa normal hata ve DIAGNOSTICS akışı kullanılır.
