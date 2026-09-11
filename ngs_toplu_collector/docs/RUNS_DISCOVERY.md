# RUNS discovery / readiness

Collector rendered NGS analiz platformu Runs DOM'unu kullanır. v0.2.4 itibarıyla parent RUN ve child sample durumları ayrı saklanır.

## Hazırlık kuralı

- RUN `COMPLETED` değilse o RUN içindeki veri kazıması yapılmaz.
- RUN tamam olsa bile sample durumu açıkça `COMPLETED` değilse sample beklemede kalır.
- Aktif durumlar (`RUNNING`, `PROCESSING`, `QUEUED`, `SUBMITTED`) `waiting` olarak tutulur.
- `ERROR`, `CANCELED`, `FAILED` `blocked` olarak tutulur.
- Sonraki RUNS taramasında durum `COMPLETED` olduğunda `waiting/blocked` görevler yeniden `pending` olur.

## POOL grubu

`POOL 59 (DNA)` ve `POOL 59 (RNA)` aynı günlük POOL grubu (`POOL 59`) olarak ele alınır. Tamamlanmış run işlenebilir; tamamlanmamış eş run beklemede kalır.
