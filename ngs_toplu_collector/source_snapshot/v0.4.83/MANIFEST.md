# v0.4.83 public full-source snapshot

- Version: `0.4.83`
- Archive: `ngs_toplu_collector_v0.4.83_PUBLIC_SOURCE.tar.xz`
- SHA-256: `8d84af1ea9a2c9ca39f2911830db2a8bb08046f37d632a9abcc4cae0e63de65f`
- Base64 chunk count: `37`
- Chunk naming: `parts/source.part000.b64` ... `source.part036.b64`
- Restore script: `RESTORE_SOURCE_v0.4.83.ps1`

Bu snapshot v0.4.82 tam kaynak çizgisi üzerine v0.4.83 değişiklikleri uygulanarak üretildi. Runtime verileri dahil değildir.

## Public güvenlik sınırı

Snapshot oluşturulurken `AI_EXPORT/`, `DIAGNOSTICS/`, `DATA/`, `OUTPUT/`, `PROFILE/`, SQLite veritabanları, browser session/cookie/token dosyaları ve yerel config dosyaları dışarıda bırakıldı. Örnek protokol/MP değerleri sentetikleştirildi. Test UUID/TC/email değerleri yalnız sentetik test verisidir.

## Geri yükleme

PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\RESTORE_SOURCE_v0.4.83.ps1
```

Script 37 parçayı birleştirir, Base64 çözer, SHA-256 doğrular ve Windows `tar.exe` varsa kaynağı açar.

## Mimari belgeler

Geliştirmeye başlamadan önce:

- `../../docs/ARCHITECTURE_v0483.md`
- `../../docs/AI_DECISION_ENGINE_v0483.md`
- `../../docs/UI_ADAPTER_CONTRACT_v0483.md`

okunmalıdır. Özellikle YZ önerisi, GenNext Selection ve patolog rapor kararı birbirine otomatik dönüştürülmemelidir.