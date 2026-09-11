# Dış varyant bağlantıları

## OncoKB

Uygulama gen + protein değişikliği varsa gene/alteration URL üretir.

Örnek:
- Gene: `BRAF`
- aaChange: `p.V600E`
- URL yolu: `/gene/BRAF/V600E`

## Franklin

Genomik deep-link için referans build kritik olduğundan varsayılan `auto` modunda Franklin arama sayfası açılır ve sorgu panoya kopyalanır.

`config.json`:

```json
"franklin_reference": "auto"
```

Desteklenen bilinçli ayar:
- `auto`: güvenli arama sayfası + pano
- `hg19`: somatik genomic deep-link kullanılabilir

Build kesin değilse `auto` bırakılmalıdır.
