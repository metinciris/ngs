# GenNext yeni domain gecisi (22 Kasim 2026)

GenNext yeni giris: https://app.gennext.bio/auth

Kullanici bildirimine gore eski veriler tasinmis ve giris kimlik bilgileri ayni. Canli NGS Mutation Pool veri toplama islemi bu commit ile test edilmedi.

## Iki baslatici

- `BASLAT_GENNEXT_YENI.bat`: `https://app.gennext.bio` taban adresi, `https://app.gennext.bio/runs?page=1&perPage=7` RUNS.
- `BASLAT_GENNEXT_ESKI.bat`: `https://beta.gennext.bio` adresine geri donus.
- `BASLAT.bat`: aynen duruyor; daha onceki yerel ayarlari kullaniyor.

Her iki ek baslatici da yalnizca **baslatilan isleme ozel** `NGS_PLATFORM_BASE_URL` ve `NGS_PLATFORM_RUNS_URL` ortam degiskenleri atar, sonra mevcut `BASLAT.bat` dosyasini cagirir. `config.local.json`, `DATA/`, `OUTPUT/`, `PROFILE/`, `AI_EXPORT/` ve yerel rapor kararlari **degistirilmez**. Ilk yeni site oturumunda giris istenebilir.

## Windows kurulumu

Yalnizca iki yeni BAT dosyasini GitHub'daki `ngs_toplu_collector/` yolundan alarak mevcut `C:\\ngs\\ngs_toplu_collector\\` klasorune kopyalayin (repo tamamini indirip mevcut kurulumun ustune yazmayin).

1. Calisan v0.4.92 uygulamasini kapatin. Yerel verilerin kurum politikasina uygun yedegini alin.
2. `BASLAT_GENNEXT_YENI.bat` calistirin; gerekiyorsa yeni sitede oturum acin.
3. RUNS'u tarayin. Eski bir DNA ve RNA teknik kaydinin kimlik, SNV/CNV, TMB/MSI/HRD, RNA Fusion ve Selection verilerini karsilastirin. Hata veya eksik alan varsa tum POOL'u yeniden toplamayin.
4. Sorun varsa uygulamayi kapatip `BASLAT_GENNEXT_ESKI.bat` ile geri donun.

> **Surum notu:** GitHub'daki tam kaynak cizgisi v0.4.83, calisan yerel referans v0.4.92. Bu baslaticilar yerel v0.4.92'nin de belirtilen ortam degiskenlerini okudugu varsayimina dayanir. Bunu yerel ortamda dogrulamak gerekir. Yeni domaine yonlendirme basarisizsa eski baslaticiya donun ve yerel kaynak/ayar duzenini kontrol edin. Yalnizca URL degisimi, GenNext API/DOM degisikliginin olmadigini garanti etmez.

Guvenlik: parola, cookie, token, hasta verisi, gercek UUID, yerel config veya SQLite GitHub'a yuklenmez.
