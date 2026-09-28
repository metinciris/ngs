# NGS Telegram botu: kurulum, doğrulama ve onarım

Bu belge Windows'taki NGS Mutasyon Havuzu Telegram agenti ve isteğe bağlı NGS Safe Downloader komutları içindir. Google Apps Script tabanlı PubMed botuyla ilişkili değildir.

> **Kaynak sınırı:** Bu açık depoda çalışan yerel agentin güncel ve eksiksiz kopyası, bot kimlik bilgileri ve kurum ayarları bulunmaz. Sıfırdan kurulum için ayrıca doğrulanmış **güncel yerel NGS/Telegram kurulum paketi** gerekir. Eski `v0.4.63` yedeğini daha yeni bir agentin üzerine kopyalamayın. Bu rehber, olmayan kodu GitHub'dan kurduğunu iddia etmez.

## 1. Başlamadan önce

- Windows 10/11 ve Python 3 (`py -3 --version` komutu çalışmalı).
- Güncel yerel agent: `C:\ngs\ngs_toplu_collector\telegram_remote_agent.py` ve aynı klasörde onun kullandığı `remote_control.py`.
- Downloader komutları kullanılacaksa ilgili güncel paket: `C:\NGS_Safe_Downloader`.
- Telegram'da BotFather ile oluşturulmuş botun **tokenı**, yetkili kişinin **sayısal user ID**'si ve kullanılacak sohbetin **sayısal chat ID**'si.
- İlk kurulum sırasında internet bağlantısı ve Telegram API erişimi.

Kod paketi eksikse önce güncel agent kaynağını kendi güvenilir yedeğinizden edinin. `ngs_toplu_collector/` klasörünü veya `C:\NGS_Safe_Downloader` klasörünü yalnız bot yanıt vermiyor diye silmeyin; yerel çalışma verileri ve ayarlar etkilenebilir.

## 2. BotFather ve kimlikler

1. Telegram'da **@BotFather** ile `/newbot` kullanarak bir bot oluşturun veya mevcut botunuzu kullanın. Bot kullanıcı adını ve tokenı güvenli yerde tutun.
2. Botla **özel sohbet** açıp `/start` gönderin. Özel sohbette user ID ile chat ID genellikle aynı sayıdır; ikisini yine de çıktıda ayrı doğrulayın.
3. Bu depodaki [`TELEGRAM_KIMLIK_BUL.py`](../tools/telegram/TELEGRAM_KIMLIK_BUL.py) aracını Windows'ta `py -3 TELEGRAM_KIMLIK_BUL.py` komutuyla çalıştırın. Token ekranda gizli girilir; araç son mesajların yalnızca sayısal user/chat ID'lerini gösterir ve tokenı dosyaya kaydetmez. Bot zaten çalışan bir agent tarafından dinleniyorsa bu aracı kullanmayın; paralel `getUpdates` çağrısı çakışabilir.
4. Tokenı, `telegram_token.dpapi` dosyasını veya gerçek hasta/NGS verilerini GitHub'a, ekran görüntüsüne ya da sohbet mesajına eklemeyin.

`/setcommands` BotFather menüsünü düzenler; menünün görünmesi Windows agentinin çalıştığını **kanıtlamaz**. `/pckapat` komutu bilinçli olarak menüde listelenmez.

## 3. Yeni agenti yerelde kurma

Bu adımları yalnız doğrulanmış **tam ve güncel** agent paketiyle uygulayın. Daha önce çalışan bot varsa doğrudan **4. Mevcut kurulumu denetleme** bölümüne geçin; `--setup` mevcut şifreli ayarları yeniden oluşturur.

1. Paket talimatına göre NGS collector ve gerekiyorsa downloader dosyalarını yukarıdaki klasörlere kurun. `telegram_remote_agent.py`, `remote_control.py` ve agentin import ettiği diğer modüllerin gerçekten bulunduğunu kontrol edin.
2. Komut İstemi'nde sözdizimini denetleyin:

   ```bat
   py -3 -m py_compile C:\ngs\ngs_toplu_collector\telegram_remote_agent.py
   ```

3. Kurulumu **botun çalışacağı aynı Windows kullanıcı hesabında** tamamlayın. Sayısal değerleri ve bot adını kendinize göre değiştirin:

   ```bat
   py -3 C:\ngs\ngs_toplu_collector\telegram_remote_agent.py --setup --allowed-user 123456789 --allowed-chat 123456789 --bot-username kendi_botunuz
   ```

   Agent tokenı gizli istemle sorar ve Windows DPAPI ile yerelde saklar. Farklı Windows hesabındaki DPAPI dosyası doğrudan kullanılamaz. Grup sohbetinde `--allowed-chat` değeri ayrı ve genellikle negatif bir sayıdır.

4. Telegram'da önce `/durum`, sonra `/yardim` deneyin. Downloader entegrasyonu gerçekten kuruluysa `/indir` ve `/indir_durum` komutlarını ayrıca deneyin. PC kapatma yaması kuruluysa `/pckapat` yalnız onay isteği verir; kapatmayı başlatmak için 60 saniye içinde `/pckapat_onay` gerekir. Test sırasında bu onayı göndermeyin.

`v0.4.63` içindeki `TAM_KURULUM_v0.4.63.bat` yalnız kendi döneminin **mevcut agentini** onarmak içindir; tek başına yeni ve güncel bir agent kurmaz. Güncel bir kurulumun üzerine eski tam kurulum/yama paketlerini sırayla uygulamayın.

## 4. Mevcut kurulumu denetleme

Komut İstemi'nde:

```bat
py -3 -m py_compile C:\ngs\ngs_toplu_collector\telegram_remote_agent.py
C:\NGS_Safe_Downloader\KONTROL_TELEGRAM_AGENT.bat
```

Kontrol çıktısında agent yolu bulunmalı ve ilgili Python süreci görünmelidir. Komut menüsü var ama `/durum` yanıtı yoksa botun çalıştığını varsaymayın. Sözdizimi sağlam olsa bile süreç durmuş, yanlış dosya başlatılmış veya `telegram_agent.lock` takılı kalmış olabilir.

`--reload-agent` komutu **"Eski Telegram agent durmadı"** derken kontrol aracı **"YOK"** diyorsa, kilitteki PID başka bir süreç tarafından yeniden kullanılmış olabilir. PID'yi rastgele `taskkill` ile kapatmayın.

Bu duruma özel [`ONAR_TELEGRAM_AGENT.bat`](../tools/telegram/ONAR_TELEGRAM_AGENT.bat) aracını çalıştırın (aynı klasördeki `.py` dosyasıyla birlikte indirin). Araç gerçek agent sürecini denetler, varsa nazikçe durmasını bekler, süreç yokken takılı kilidi `.bak` olarak yedekler ve **mevcut** agenti yeniden başlatır. Kod, token ve NGS verilerini silmez. Başarıdan sonra Telegram'da `/durum` deneyin. Hata verirse ekrandaki hata metnini ve `C:\ngs\ngs_toplu_collector\telegram_remote_agent_repair.log` dosyasının son satırlarını inceleyin; token dosyasını paylaşmayın.

Bu araç güncel agenti **oluşturmaz**; dosya veya bağımlılık eksikse durur. Agent çalışıyor ama durum kaydında `network_error` varsa ağ/Telegram API veya agentin mesaj döngüsü ayrıca araştırılmalıdır.

## 5. Hızlı sorun tablosu

| Belirti | İlk kontrol | Eylem |
| --- | --- | --- |
| Menü görünüyor, yanıt yok | `KONTROL_TELEGRAM_AGENT.bat` ve `/durum` | Agent süreci yoksa kilit onarımını çalıştırın. |
| `py_compile` hata veriyor | Hata satırı ve dosyanın yedeği | Eski paketi tekrar uygulamayın; sözdizimi düzeltilmeden başlatmayın. |
| `--reload-agent` eski agentin durmadığını söylüyor, süreç yok | Takılı `telegram_agent.lock` | Yukarıdaki onarım aracını kullanın. |
| Agent var, `network_error` görünüyor | Ağ/Telegram erişimi ve başlangıç günlüğü | Hata metnini inceleyin; tokenı yazdırmayın. |
| `/indir` çalışıyor ama gönderilen bağlantıya yanıt yok | Agent/indir sürüm uyumu | Güncel `ngs_telegram_download_extension.py` ve komut entegrasyonunu kontrol edin; bağlantı tokenını paylaşmayın. |
| Bot başka Windows hesabında çalışmıyor | DPAPI kullanıcı hesabı | Tokenı o hesapta yeniden güvenli kurulumla girin; şifreli dosyayı taşımayın. |

## 6. Başka bir YZ/asistan için devralma notu

> Hedef `metinciris/ngs` deposundaki NGS collector ve ayrı Safe Downloader sidecar'ıdır. Canlı Windows yolları `C:\ngs\ngs_toplu_collector` ve `C:\NGS_Safe_Downloader` olabilir; önce kullanıcı bilgisayarı üzerinde doğrula. GitHub'daki açık kaynak kopya, güncel Telegram agentinin eksiksiz dağıtımı değildir. Yerel `telegram_remote_agent.py` ve aynı sürüme ait bağımlılıkları görmeden eski yedekle yeniden oluşturma. Bot tokenını ve DPAPI/config/state dosyalarını isteme veya yayımlama. Menü varlığı agent sağlığı kanıtı değildir; sözdizimi, gerçek Python süreci, kilit ve durum dosyasını sırayla incele. Takılı kilitte PID yeniden kullanılmış olabilir: başka bir süreci öldürme; `tools/telegram/ONAR_TELEGRAM_AGENT.py` güvenli süreç kontrolü ve kilit yedeği yapar. Telegram yanıtı için `/durum` ile uçtan uca doğrulama iste. `/pckapat` menüde değildir ve ayrıca `/pckapat_onay` gerektirir. Downloader çekirdeği v2.6 ile Telegram sidecar sürümlerini ayrı tut; özel kurum URL'leri, hasta verileri ve tokenları açık depoya koyma.

Bu belge, gerçek yerel kurulumdan alınan dosyaların yerini tutmaz. Yerel ve GitHub sürümleri ayrışıyorsa önce farkları saptayıp yalnız doğrulanmış kaynak üzerinden ilerleyin.
