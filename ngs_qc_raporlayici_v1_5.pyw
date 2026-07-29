# -*- coding: utf-8 -*-
"""
NGS QC Raporlayici v1.5

- Zebra/FASTQ kalite HTML veya metin raporlarini okur.
- Cutadapt raporlarini ayni ornek altinda birlestirir.
- Daha once olusturulmus QC ozet CSV dosyalarini da okuyabilir.
- DNA ve RNA icin rapora eklenebilir kisa Turkce cumle uretir.
- ZIP arsivlerini dogrudan acar.
- RAR arsivlerini Windows'ta kurulu WinRAR/UnRAR/7-Zip ile acar.

Yalnizca Python standart kutuphanesini kullanir.
Python 3.10+ onerilir.
"""

from __future__ import annotations

import csv
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "NGS QC Raporlayıcı 1.5"
SUPPORTED_REPORT_EXTENSIONS = {".html", ".htm", ".txt", ".log", ".report", ".csv", ".tsv"}
ARCHIVE_EXTENSIONS = {".zip", ".rar"}
IGNORED_LARGE_EXTENSIONS = {".fastq", ".fq", ".gz", ".bam", ".cram", ".vcf"}


def compact_text(value: str) -> str:
    """Cok satirli sabit metinleri tek, duzgun paragrafa cevir."""
    return " ".join(value.split())


DNA_METHOD_TEXT = (
    "Tedavi planlaması için NGS (yeni nesil dizileme) yöntemi ile "
    "'DNA Tümör Paneli' çalışması yapılmıştır."
)

RNA_METHOD_TEXT = (
    "Tedavi planlaması için NGS (yeni nesil dizileme) yöntemi ile "
    "'Genel Füzyon Paneli' çalışması yapılmıştır."
)

MSI_METHOD_TEXT = (
    "- Mikrosatellit instabilite durumu, Kapsamlı DNA Kanser Paneli kapsamında "
    "değerlendirilen informatif mikrosatellit bölgeleri üzerinden, validasyonu yapılmış "
    "biyoinformatik analiz algoritması ile değerlendirilmiştir."
)

DNA_PANEL_TEXT = compact_text("""
- ABL1, ABL2, ABR, ACVR1, ACVR1B, ACVR2A, ADGRA2, AJUBA, AKAP9, AKT1, AKT2, AKT3, ALK, ALOX12B, ALOX15B, AMER1, ANKRD11, ANKRD26, APC, APLNR, AR, ARAF, ARFRP1, ARHGAP26, ARHGAP35, ARID1A, ARID1B, ARID2, ARID5B, ASXL1, ASXL2, ATM, ATR, ATRX, AURKA, AURKB, AURKC, AXIN1, AXIN2, AXL, B2M, BAP1, BARD1, BBC3, BCL10, BCL2, BCL2L1, BCL2L11, BCL2L2, BCL6, BCOR, BCORL1, BCR, BIRC2, BIRC3, BLM, BMPR1A, BRAF, BRCA1, BRCA2, BRD4, BRIP1, BTG1, BTG2, BTK, C11ORF30, CALR, CARD11, CASP8, CASR, CBFB, CBL, CBLB, CCND1, CCND2, CCND3, CCNE1, CD22, CD274, CD276, CD38, CD44, CD58, CD70, CD74, CD79A, CD79B, CDC73, CDH1, CDK12, CDK2, CDK4, CDK6, CDK7, CDK8, CDKN1A, CDKN1B, CDKN1C, CDKN2A, CDKN2B, CDKN2C, CEBPA, CENPA, CFTR, CHD2, CHD4, CHD8, CHEK1, CHEK2, CIC, CIITA, CKS1B, COL17A1, CPA1, CRBN, CREBBP, CRKL, CRLF2, CSAD, CSF1R, CSF3R, CSNK1A1, CTCF, CTLA4, CTNNA1, CTNNB1, CTRC, CUL3, CUL4A, CUL4B, CUX1, CXCR4, CYLD, CYP17A1, DAXX, DCUN1D1, DDR1, DDR2, DDX3X, DDX41, DDX5, DEFB134, DHX15, DHX9, DICER1, DIS3, DIS3L2, DLX1, DNAJB1, DNMT1, DNMT3A, DNMT3B, DOT1L, DPYD, E2F3, EED, EGFL7, EGFR, EIF1AX, EIF4A2, EIF4E, ELAC2, ELF3, EML4, EP300, EPCAM, EPHA2, EPHA3, EPHA5, EPHA7, EPHB1, EPHB2, EPHB4, ERBB2, ERBB3, ERBB4, ERCC1, ERCC2, ERCC3, ERCC4, ERCC5, ERG, ERRFI1, ESR1, ESR2, ETS1, ETV1, ETV4, ETV5, ETV6, EWSR1, EZH2, EZR, FAM175A, FAM46C, FANCA, FANCC, FANCD2, FANCE, FANCF, FANCG, FANCI, FANCL, FANCM, FAS, FAT1, FBXO11, FBXW7, FGF1, FGF10, FGF12, FGF14, FGF19, FGF2, FGF23, FGF3, FGF4, FGF5, FGF6, FGF7, FGF8, FGF9, FGFR1, FGFR2, FGFR3, FGFR4, FH, FLCN, FLI1, FLT1, FLT3, FLT4, FOXA1, FOXA2, FOXL2, FOXO1, FOXP1, FRS2, FUBP1, FYN, GABRA6, GATA1, GATA2, GATA3, GATA4, GATA6, GEN1, GID4, GLI1, GNA11, GNA13, GNAI2, GNAQ, GNAS, GPC3, GPS2, GRB2, GREM1, GRIN2A, GRM3, GSK3B, H3F3A, H3F3B, H3F3C, HDAC1, HGF, HIF1A, HIST1H1C, HIST1H2BD, HIST1H3A, HIST1H3B, HIST1H3C, HIST1H3D, HIST1H3E, HIST1H3F, HIST1H3G, HIST1H3H, HIST1H3I, HIST1H3J, HIST2H3C, HIST2H3D, HIST3H3, HLA-A, HLA-B, HLA-C, HNF1A, HNRNPK, HOXB13, HOXC6, HRAS, HSD3B1, HSP90AA1, ICOSLG, ID3, IDH1, IDH2, IDO1, IDO2, IFNGR1, IFNGR2, IGF1, IGF1R, IGF2, IKBKE, IKZF1, IKZF3, IL10, IL6R, IL6ST, IL7R, ING1, INHA, INHBA, INPP4A, INPP4B, INSR, IRF1, IRF2, IRF4, IRS1, IRS2, JAK1, JAK2, JAK3, JUN, KAT6A, KDM5A, KDM5C, KDM6A, KDR, KEAP1, KEL, KIAA1549, KIF5B, KIT, KLF2, KLF4, KLHL6, KMT2A, KMT2B, KMT2C, KMT2D, KRAS, LAMP1, LATS1, LATS2, LMO1, LRP1B, LTK, LYN, LZTR1, MAF, MAGEC3, MAGI2, MALT1, MAML2, MAP2K1, MAP2K2, MAP2K4, MAP3K1, MAP3K13, MAP3K14, MAP3K4, MAP3K7, MAPK1, MAPK3, MAX, MC1R, MCL1, MDC1, MDM2, MDM4, MECOM, MED12, MEF2B, MEN1, MERTK, MET, MGA, MGMT, MITF, MKNK1, MLH1, MLLT3, MPL, MRE11A, MSH2, MSH3, MSH6, MST1, MST1R, MTAP, MTOR, MUTYH, MYB, MYC, MYCL, MYCN, MYD88, MYH9, MYOD1, NAB2, NBN, NCOA2, NCOA3, NCOR1, NCOR2, NEGR1, NF1, NF2, NFE2L2, NFKB2, NFKBIA, NKX2-1, NKX3-1, NLRC5, NOTCH1, NOTCH2, NOTCH3, NOTCH4, NPM1, NR3C1, NRAS, NRG1, NSD1, NT5C2, NTHL1, NTRK1, NTRK2, NTRK3, NUP93, NUTM1, P2RY8, PAK1, PAK3, PAK7, PALB2, PARK2, PARP1, PARP2, PARP3, PAX3, PAX5, PAX7, PAX8, PBRM1, PCBP1, PDCD1, PDCD1LG2, PDGFRA, PDGFRB, PDK1, PDPK1, PGR, PHF6, PHOX2B, PIAS3, PIAS4, PIK3C2B, PIK3C2G, PIK3C3, PIK3CA, PIK3CB, PIK3CD, PIK3CG, PIK3R1, PIK3R2, PIK3R3, PIM1, PIM2, PIM3, PLCG1, PLCG2, PLK2, PMAIP1, PML, PMS1, PMS2, PNRC1, POLD1, POLE, POLQ, POT1, PPARG, PPM1D, PPP2R1A, PPP2R2A, PPP4R2, PPP6C, PRAME, PRC1, PRDM1, PREX2, PRKAR1A, PRKCI, PRKDC, PRSS1, PRSS8, PSIP1, PSMA1, PSMB5, PSMD1, PSMG2, PTCH1, PTEN, PTK2, PTPN11, PTPRD, PTPRO, PTPRS, PTPRT, QKI, QSER1, RAB35, RAC1, RAD21, RAD50, RAD51, RAD51B, RAD51C, RAD51D, RAD52, RAD54L, RAF1, RANBP2, RARA, RASA1, RB1, RBM10, RECQL4, REL, REST, RET, RFWD2, RFX5, RFXAP, RHEB, RHOA, RICTOR, RIT1, RNASEL, RNF43, ROS1, RPL22, RPL5, RPS6KA4, RPS6KB1, RPS6KB2, RPTOR, RRM1, RSPO2, RUNX1, RUNX1T1, RXRA, RYBP, SDC4, SDHA, SDHAF2, SDHB, SDHC, SDHD, SERPINB3, SERPINB4, SETBP1, SETD2, SF3B1, SGK1, SH2B3, SH2D1A, SHQ1, SIN3A, SLC34A2, SLIT2, SLX4, SMAD2, SMAD3, SMAD4, SMARCA4, SMARCB1, SMARCD1, SMARCE1, SMC1A, SMC3, SMG1, SMO, SNCAIP, SOCS1, SOS1, SOX10, SOX17, SOX2, SOX9, SPEN, SPINK1, SPOP, SPTA1, SRC, SRSF2, STAG1, STAG2, STAT1, STAT3, STAT4, STAT5A, STAT5B, STAT6, STK11, STK40, SUFU, SUZ12, SYK, TAF1, TAF3, TAP1, TAP2, TAPBP, TBL1XR1, TBX3, TCEB1, TCF12, TCF3, TCF7L2, TEK, TERC, TERT, TET1, TET2, TFE3, TFEB, TFRC, TGFBR1, TGFBR2, TIPARP, TLR4, TMEM127, TMPRSS2, TNFAIP3, TNFRSF14, TOP1, TOP2A, TP53, TP53BP1, TP63, TP73, TRAF2, TRAF3, TRAF7, TSC1, TSC2, TSHR, TYR, TYRO3, U2AF1, UGT1A1, UVRAG, VEGFA, VHL, VTCN1, WHSC1, WHSC1L1, WISP3, WRN, WT1, XBP1, XIAP, XPO1, XRCC2, YAP1, YES1, ZBTB2, ZBTB7A, ZFHX3, ZFP36L1, ZMYM2, ZMYM3, ZNF217, ZNF703, ZNF750, ZRSR2 genlerini içeren DNA Tümör Paneli, NGS yöntemi ile dizi analizi yapılarak incelendi.
""")

RNA_PANEL_TEXT = compact_text("""
- ABL1, AKT3, ALK, AR, ARHGAP26, AXL, BCL2, BRAF, BRCA1, BRCA2, BRD3, BRD4, CDK4, CIC, CSF1R, EGFR, EML4, ERBB2, ERG, ESR1, ETS1, ETV1, ETV4, ETV5, ETV6, EWSR1, FGFR1, FGFR2, FGFR3, FGFR4, FGR, FLI1, FLT1, FLT3, INSR, JAK2, KDR, KIF5B, KIT, KMT2A, MAML2, MAST1, MAST2, MET, MLLT3, MSH2, MSMB, MUSK, MYB, MYC, NOTCH1, NOTCH2, NOTCH3, NRG1, NTRK1, NTRK2, NTRK3, NUMBL, NUTM1, PAX3, PAX7, PDGFRA, PDGFRB, PIK3CA, PKN1, PPARG, PRKCA, PRKCB, RAF1, RELA, RET, ROS1, RPS6KB1, RSPO2, RSPO3, TERT, TFE3, TFEB, THADA, TMPRSS2 genlerini içeren Genel Füzyon Paneli, NGS yöntemi ile dizi analizi yapılarak incelendi.
""")

DNA_NEGATIVE_GENES = ("BRAF", "EGFR", "KRAS", "NRAS", "TERT")
DNA_NEGATIVE_TEMPLATE = "- {gene} geninde dizi analizi yöntemi ile patojenik mutasyon saptanmamıştır."

METHOD_DETAILS_TEXT = "- Yöntemin detayları ve uygulanan testin klinik önemi ek raporda bildirilmiştir."
OTHER_GENES_NEGATIVE_TEXT = "- Patolojik boyamada bildirilen diğer genlerde patolojik mutasyon saptanmamıştır."

RNA_PANEL_GENES = tuple("""ABL1 AKT3 ALK AR ARHGAP26 AXL BCL2 BRAF BRCA1 BRCA2 BRD3 BRD4 CDK4 CIC CSF1R EGFR EML4 ERBB2 ERG ESR1 ETS1 ETV1 ETV4 ETV5 ETV6 EWSR1 FGFR1 FGFR2 FGFR3 FGFR4 FGR FLI1 FLT1 FLT3 INSR JAK2 KDR KIF5B KIT KMT2A MAML2 MAST1 MAST2 MET MLLT3 MSH2 MSMB MUSK MYB MYC NOTCH1 NOTCH2 NOTCH3 NRG1 NTRK1 NTRK2 NTRK3 NUMBL NUTM1 PAX3 PAX7 PDGFRA PDGFRB PIK3CA PKN1 PPARG PRKCA PRKCB RAF1 RELA RET ROS1 RPS6KB1 RSPO2 RSPO3 TERT TFE3 TFEB THADA TMPRSS2""".split())
RNA_GENERAL_NEGATIVE_GENES = tuple("""ABL1 AKT3 ALK AR ARHGAP26 AXL BCL2 BRAF BRCA1 BRCA2 BRD3 BRD4 CDK4 CIC CSF1R EGFR EML4 ERBB2 ERG ESR1 ETS1 ETV1 ETV4 ETV5 ETV6 EWSR1 FGFR4 FGR FLI1 FLT1 FLT3 INSR JAK2 KDR KIF5B KIT KMT2A MAML2 MAST1 MAST2 MET MLLT3 MSH2 MSMB MUSK MYB MYC NOTCH1 NOTCH2 NOTCH3 NRG1 NUMBL NUTM1 PAX3 PAX7 PDGFRA PDGFRB PIK3CA PKN1 PPARG PRKCA PRKCB RAF1 RELA RET RPS6KB1 RSPO2 RSPO3 TERT TFE3 TFEB THADA TMPRSS2""".split())


def is_candidate_report(path: Path) -> bool:
    """Dosya adina gore okunabilir QC raporlarini sec."""
    name = path.name.lower()
    return (
        path.is_file()
        and not name.endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz", ".bam", ".cram", ".vcf", ".vcf.gz"))
        and (
            path.suffix.lower() in SUPPORTED_REPORT_EXTENSIONS
            or name.endswith(".trimming.report")
            or ".fqstat." in name
            or name.endswith("fqstat")
        )
    )


NUMBER_RE = r"(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"


@dataclass
class SampleMetrics:
    sample: str
    sample_type: str = ""  # DNA / RNA
    raw_pairs: Optional[int] = None
    total_reads_m: Optional[float] = None  # paired-end read pairs, millions
    q30_pct: Optional[float] = None
    r1_q30_pct: Optional[float] = None
    r2_q30_pct: Optional[float] = None
    gc_pct: Optional[float] = None
    r1_gc_pct: Optional[float] = None
    r2_gc_pct: Optional[float] = None
    clean_bases_gb: Optional[float] = None
    raw_bases_gb: Optional[float] = None
    base_retention_pct: Optional[float] = None
    pass_pairs: Optional[int] = None
    pass_pct: Optional[float] = None
    source_files: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "SampleMetrics") -> None:
        if not self.sample_type and other.sample_type:
            self.sample_type = other.sample_type
        for attr in (
            "raw_pairs", "total_reads_m", "q30_pct", "r1_q30_pct", "r2_q30_pct",
            "gc_pct", "r1_gc_pct", "r2_gc_pct", "clean_bases_gb", "raw_bases_gb", "base_retention_pct",
            "pass_pairs", "pass_pct",
        ):
            current = getattr(self, attr)
            incoming = getattr(other, attr)
            if current is None and incoming is not None:
                setattr(self, attr, incoming)
        self.source_files.update(other.source_files)
        for note in other.notes:
            if note not in self.notes:
                self.notes.append(note)

    def finalize(self) -> None:
        if self.raw_pairs is not None and self.total_reads_m is None:
            self.total_reads_m = self.raw_pairs / 1_000_000
        if self.total_reads_m is not None and self.raw_pairs is None:
            self.raw_pairs = round(self.total_reads_m * 1_000_000)
        if self.q30_pct is None and self.r1_q30_pct is not None and self.r2_q30_pct is not None:
            self.q30_pct = (self.r1_q30_pct + self.r2_q30_pct) / 2
        if self.gc_pct is None and self.r1_gc_pct is not None and self.r2_gc_pct is not None:
            self.gc_pct = (self.r1_gc_pct + self.r2_gc_pct) / 2
        # Ham ve temiz baz sayilari varsa yuvarlatilmis rapor yüzdesi yerine
        # bunlardan daha hassas baz korunma oranini hesapla.
        if self.clean_bases_gb is not None and self.raw_bases_gb not in (None, 0):
            self.base_retention_pct = 100 * self.clean_bases_gb / self.raw_bases_gb
        if self.pass_pairs is not None and self.raw_pairs not in (None, 0):
            self.pass_pct = 100 * self.pass_pairs / self.raw_pairs

    def missing_required(self) -> list[str]:
        required = [
            ("okuma çifti", self.total_reads_m),
            ("Q30", self.q30_pct),
            ("temiz veri", self.clean_bases_gb),
            ("baz korunumu", self.base_retention_pct),
        ]
        if self.sample_type == "DNA":
            required.insert(2, ("GC", self.gc_pct))
        return [name for name, value in required if value is None]

    def report_sentence(self) -> str:
        self.finalize()
        molecule = self.sample_type if self.sample_type in {"DNA", "RNA"} else "NGS"
        clauses: list[str] = []

        if self.total_reads_m is not None:
            clauses.append(
                f"{molecule} dizilemesinde {tr_num(self.total_reads_m, 2)} milyon "
                "paired-end okuma çifti elde edilmiştir."
            )
        else:
            clauses.append(f"{molecule} dizilemesi gerçekleştirilmiştir.")

        quality_parts: list[str] = []
        if self.q30_pct is not None:
            quality_parts.append(f"Q30 baz oranı %{tr_num(self.q30_pct, 2)}")
        if molecule == "DNA" and self.gc_pct is not None:
            quality_parts.append(f"GC oranı %{tr_num(self.gc_pct, 2)}")
        if self.clean_bases_gb is not None:
            quality_parts.append(
                f"trimming sonrası temiz veri miktarı {tr_num(self.clean_bases_gb, 2)} Gb"
            )

        if quality_parts:
            if len(quality_parts) == 1:
                clauses.append(quality_parts[0] + " olarak bulunmuştur.")
            else:
                clauses.append(
                    ", ".join(quality_parts[:-1]) + " ve " + quality_parts[-1] + " olarak bulunmuştur."
                )

        if self.base_retention_pct is not None:
            clauses.append(
                f"Baz korunma oranı %{tr_num(self.base_retention_pct, 2)} olarak bulunmuştur."
            )

        return " ".join(clauses)


def tr_num(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def parse_number(raw: str) -> float:
    s = raw.strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        raise ValueError("Boş sayı")

    comma_count = s.count(",")
    dot_count = s.count(".")

    if comma_count and dot_count:
        # Son ayiraci ondalik kabul et.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif comma_count > 1:
        # Cutadapt: 57,352,895
        s = s.replace(",", "")
    elif dot_count > 1:
        s = s.replace(".", "")
    elif comma_count == 1:
        left, right = s.split(",")
        # Tek virgüllü büyük sayimlarda binlik; küçük metriklerde ondalik.
        if len(right) == 3 and left.isdigit() and int(left) > 100:
            s = left + right
        else:
            s = left + "." + right
    elif dot_count == 1:
        # MGI fqStat: 97.080, 46.602. Nokta ondalik ayiracidir.
        pass

    return float(s)


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1254", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def html_to_text(raw: str) -> str:
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[\t\r ]+", " ", cleaned)
    cleaned = re.sub(r"\n+", "\n", cleaned)
    return cleaned


def first_number(text: str, patterns: Iterable[str], flags: int = re.I | re.S) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            try:
                return parse_number(match.group(1))
            except (ValueError, IndexError):
                continue
    return None


def first_integer(text: str, patterns: Iterable[str]) -> Optional[int]:
    value = first_number(text, patterns)
    return round(value) if value is not None else None


def percent_from_line(text: str, label_pattern: str) -> Optional[float]:
    pattern = rf"{label_pattern}[^\n%]{{0,160}}\(({NUMBER_RE})\s*%\)"
    return first_number(text, [pattern], flags=re.I)


def infer_sample(path: Path, text: str = "") -> tuple[str, str]:
    """
    Altium kimligini belirle.

    Oncelik:
    1. Icerikte acikca yazilmis ve D/R iceren ornek/protokol kimligi
    2. Icerikte gecen Altium dosya kalibi (99D_L01_227 gibi)
    3. Dosya adinin basi (99D_L01_227...)
    4. Dosya adinin herhangi bir yeri

    99D -> protokol 99, DNA; 99R -> protokol 99, RNA.
    """
    def make_candidate(protocol: str, marker: str) -> tuple[str, str]:
        marker = marker.upper()
        return f"{int(protocol)}{marker}", ("DNA" if marker == "D" else "RNA")

    # Acik etiketli kimlikler: Sample ID: 99D, Protocol: 99R vb.
    explicit_patterns = [
        r"(?:sample|specimen|protocol|protokol|ornek|örnek)(?:\s*(?:id|no|number|name|adi|adı|numarasi|numarası))?\s*[:=]\s*(\d{1,10})\s*([DR])\b",
        r'"(?:sample|specimen|protocol|protokol)(?:_id|_name|_no)?"\s*:\s*"?(\d{1,10})\s*([DR])\b',
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return make_candidate(match.group(1), match.group(2))

    # Icerikte komut satiri veya kaynak FASTQ adi varsa.
    match = re.search(r"(?<![A-Za-z0-9])(\d{1,10})([DR])_L\d{1,3}_\d+", text, re.I)
    if match:
        return make_candidate(match.group(1), match.group(2))

    # Dosya adinin basi asil ve guvenilir geri donus yoludur.
    match = re.match(r"^\s*(\d{1,10})([DR])(?=[_.\-]|$)", path.name, re.I)
    if match:
        return make_candidate(match.group(1), match.group(2))

    # Alt klasor veya daha karmasik adlarda ara.
    for part in [path.name, path.stem] + list(reversed(path.parts[-5:-1])):
        match = re.search(r"(?<![A-Za-z0-9])(\d{1,10})([DR])(?=[^A-Za-z0-9]|$)", part, re.I)
        if match:
            return make_candidate(match.group(1), match.group(2))

    # Son care: etiketli yalniz protokol numarasi + DNA/RNA ifadesi.
    protocol_match = re.search(
        r"(?:protocol|protokol|sample|ornek|örnek)(?:\s*(?:id|no|number|numarasi|numarası))?\s*[:=]\s*(\d{1,10})\b",
        text,
        re.I,
    )
    type_match = re.search(r"\b(DNA|RNA)\b", text[:5000], re.I)
    if protocol_match and type_match:
        marker = "D" if type_match.group(1).upper() == "DNA" else "R"
        return make_candidate(protocol_match.group(1), marker)

    stem = re.sub(r"(?i)(?:_R?[12]|\.R?[12]|[-_]read[12]|[-_]fastqc|[-_]cutadapt).*$", "", path.stem)
    stem = re.sub(r"(?i)[-_](?:report|summary|statistics|stats|quality|qc)$", "", stem)
    candidate = stem.strip(" _.-") or path.stem
    sample_type = ""
    if re.search(r"\bDNA\b", text[:5000], re.I):
        sample_type = "DNA"
    elif re.search(r"\bRNA\b", text[:5000], re.I):
        sample_type = "RNA"
    return candidate.upper(), sample_type


def parse_csv_file(path: Path) -> list[SampleMetrics]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[SampleMetrics] = []
    raw = read_text(path)
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    reader = csv.DictReader(raw.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        return []

    lower_fields = {field.lower().strip(): field for field in reader.fieldnames if field}
    if "sample" not in lower_fields and "sample_id" not in lower_fields:
        return []

    def get(row: dict[str, str], *names: str) -> Optional[str]:
        for name in names:
            actual = lower_fields.get(name.lower())
            if actual and row.get(actual) not in (None, ""):
                return row[actual]
        return None

    def num(row: dict[str, str], *names: str) -> Optional[float]:
        raw_value = get(row, *names)
        if raw_value is None:
            return None
        try:
            return parse_number(raw_value)
        except ValueError:
            return None

    for row in reader:
        sample = get(row, "sample", "sample_id")
        if not sample:
            continue
        sample_type = (get(row, "type", "sample_type") or "").upper()
        metrics = SampleMetrics(sample=sample.strip().upper(), sample_type=sample_type)
        metrics.total_reads_m = num(row, "total_reads_m", "read_pairs_m", "reads_m")
        raw_pairs = num(row, "raw_pairs", "total_read_pairs")
        metrics.raw_pairs = round(raw_pairs) if raw_pairs is not None else None
        metrics.q30_pct = num(row, "run_q30_pct", "q30_pct", "q30")
        metrics.r1_q30_pct = num(row, "r1_q30_pct")
        metrics.r2_q30_pct = num(row, "r2_q30_pct")
        metrics.gc_pct = num(row, "gc_pct", "gc")
        metrics.r1_gc_pct = num(row, "r1_gc_pct")
        metrics.r2_gc_pct = num(row, "r2_gc_pct")
        metrics.clean_bases_gb = num(row, "clean_bases_gb", "clean_gb")
        metrics.raw_bases_gb = num(row, "raw_bases_gb", "raw_gb")
        metrics.base_retention_pct = num(row, "base_retention_pct", "retention_pct")
        pass_pairs = num(row, "pass_pairs")
        metrics.pass_pairs = round(pass_pairs) if pass_pairs is not None else None
        metrics.pass_pct = num(row, "pass_pct")
        metrics.source_files.add(str(path))
        metrics.finalize()
        rows.append(metrics)
    return rows


def parse_report_file(path: Path) -> Optional[SampleMetrics]:
    if not is_candidate_report(path):
        return None
    if path.suffix.lower() in {".csv", ".tsv"}:
        return None

    raw = read_text(path)
    text = html_to_text(raw) if path.suffix.lower() in {".html", ".htm"} else raw
    searchable = raw + "\n" + text
    sample, sample_type = infer_sample(path, searchable)
    metrics = SampleMetrics(sample=sample, sample_type=sample_type)
    metrics.source_files.add(str(path))

    name_lower = path.name.lower()
    looks_r1 = bool(re.search(r"(?:^|[_.-])(?:r|read)?1(?:[_.-]|$)", name_lower))
    looks_r2 = bool(re.search(r"(?:^|[_.-])(?:r|read)?2(?:[_.-]|$)", name_lower))

    # MGI/DNBSEQ fq.fqStat.txt biçimi.
    fq_read_num = first_integer(searchable, [
        rf"(?m)^\s*#ReadNum\s+({NUMBER_RE})\s*$",
    ])
    fq_q30 = first_number(searchable, [
        rf"(?m)^\s*#Q30%\s+({NUMBER_RE})\s*$",
    ], flags=re.I | re.M)
    fq_gc = first_number(searchable, [
        rf"(?m)^\s*#GC%\s+({NUMBER_RE})\s*$",
    ], flags=re.I | re.M)

    if fq_read_num is not None:
        metrics.raw_pairs = fq_read_num
    if looks_r1 and not looks_r2:
        metrics.r1_q30_pct = fq_q30
        metrics.r1_gc_pct = fq_gc
    elif looks_r2 and not looks_r1:
        metrics.r2_q30_pct = fq_q30
        metrics.r2_gc_pct = fq_gc
    else:
        metrics.q30_pct = fq_q30
        metrics.gc_pct = fq_gc

    # Cutadapt trimming raporu.
    cutadapt_raw_pairs = first_integer(searchable, [
        rf"Total\s+read\s+pairs\s+processed\s*:\s*({NUMBER_RE})",
        rf"Total\s+paired(?:-end)?\s+reads?\s+processed\s*:\s*({NUMBER_RE})",
    ])
    if cutadapt_raw_pairs is not None:
        metrics.raw_pairs = cutadapt_raw_pairs

    metrics.pass_pairs = first_integer(searchable, [
        rf"Pairs\s+written\s*\(passing\s+filters\)\s*:\s*({NUMBER_RE})",
        rf"Read\s+pairs\s+written[^:]*:\s*({NUMBER_RE})",
    ])
    metrics.pass_pct = percent_from_line(searchable, r"Pairs\s+written\s*\(passing\s+filters\)")

    raw_bases = first_number(searchable, [
        rf"Total\s+basepairs\s+processed\s*:\s*({NUMBER_RE})\s*(?:bp|base)",
        rf"Total\s+bases\s+processed\s*:\s*({NUMBER_RE})",
    ])
    clean_bases = first_number(searchable, [
        rf"Total\s+written\s*\(filtered\)\s*:\s*({NUMBER_RE})\s*(?:bp|base)",
        rf"Total\s+basepairs\s+written[^:]*:\s*({NUMBER_RE})",
        rf"Clean\s+bases?\s*[:=]\s*({NUMBER_RE})\s*(?:bp|base)",
    ])
    if raw_bases is not None:
        metrics.raw_bases_gb = raw_bases / 1_000_000_000
    if clean_bases is not None:
        metrics.clean_bases_gb = clean_bases / 1_000_000_000

    metrics.base_retention_pct = percent_from_line(
        searchable, r"Total\s+written\s*\(filtered\)"
    )

    # Zebra HTML: summaryTable ve fqTable JavaScript dizileri.
    html_total_reads_m = first_number(searchable, [
        rf"['\"]TotalReads\(M\)['\"]\s*,\s*['\"]({NUMBER_RE})['\"]",
        rf"Total\s+Reads?\s*\(M\)\s*[:=]?\s*({NUMBER_RE})",
        rf"Total[ _-]*reads[ _-]*m\s*[:=]\s*({NUMBER_RE})",
        rf'"total_reads_m"\s*:\s*({NUMBER_RE})',
    ])
    if html_total_reads_m is not None:
        metrics.total_reads_m = html_total_reads_m

    generic_q30 = first_number(searchable, [
        rf"['\"]Q30\(%\)['\"]\s*,\s*['\"]({NUMBER_RE})['\"]",
        rf"(?:run|overall|total|all)[ _-]*q30(?:[ _-]*(?:pct|percent|percentage|rate|bases?))?\s*(?:[:=]\s*)?({NUMBER_RE})\s*%?",
        rf"q30(?:[ _-]*(?:pct|percent|percentage|rate|bases?))?\s*(?:[:=]\s*)?({NUMBER_RE})\s*%",
        rf"Q30\s*\(%\)\s*(?:[:=]\s*)?({NUMBER_RE})",
        rf'"(?:run_)?q30(?:_pct)?"\s*:\s*({NUMBER_RE})',
    ])
    if looks_r1 and not looks_r2:
        if metrics.r1_q30_pct is None:
            metrics.r1_q30_pct = generic_q30
    elif looks_r2 and not looks_r1:
        if metrics.r2_q30_pct is None:
            metrics.r2_q30_pct = generic_q30
    elif metrics.q30_pct is None:
        metrics.q30_pct = generic_q30

    metrics.r1_q30_pct = metrics.r1_q30_pct or first_number(searchable, [
        rf"(?:R1|Read\s*1)[ _-]*Q30(?:[ _-]*(?:pct|rate|bases?))?\s*[:=]\s*({NUMBER_RE})\s*%?",
        rf'"r1_q30(?:_pct)?"\s*:\s*({NUMBER_RE})',
    ])
    metrics.r2_q30_pct = metrics.r2_q30_pct or first_number(searchable, [
        rf"(?:R2|Read\s*2)[ _-]*Q30(?:[ _-]*(?:pct|rate|bases?))?\s*[:=]\s*({NUMBER_RE})\s*%?",
        rf'"r2_q30(?:_pct)?"\s*:\s*({NUMBER_RE})',
    ])

    generic_gc = first_number(searchable, [
        # read total satiri: PhredQual, ReadNum, BaseNum, N%, GC%, ...
        rf"['\"]read\s+total['\"]\s*,\s*['\"]{NUMBER_RE}['\"]\s*,\s*['\"]{NUMBER_RE}['\"]\s*,\s*['\"]{NUMBER_RE}['\"]\s*,\s*['\"]{NUMBER_RE}['\"]\s*,\s*['\"]({NUMBER_RE})['\"]",
        rf"(?:overall|total|all)[ _-]*GC(?:[ _-]*(?:content|pct|percent|rate))?\s*(?:[:=]\s*)?({NUMBER_RE})\s*%?",
        rf"GC(?:[ _-]*(?:content|pct|percent|rate))?\s*(?:[:=]\s*)?({NUMBER_RE})\s*%",
        rf"GC\s*\(%\)\s*(?:[:=]\s*)?({NUMBER_RE})",
        rf'"gc(?:_pct|_content)?"\s*:\s*({NUMBER_RE})',
    ])
    if looks_r1 and not looks_r2:
        if metrics.r1_gc_pct is None:
            metrics.r1_gc_pct = generic_gc
    elif looks_r2 and not looks_r1:
        if metrics.r2_gc_pct is None:
            metrics.r2_gc_pct = generic_gc
    elif metrics.gc_pct is None:
        metrics.gc_pct = generic_gc

    metrics.finalize()

    meaningful = any(
        value is not None
        for value in (
            metrics.raw_pairs, metrics.total_reads_m, metrics.q30_pct,
            metrics.r1_q30_pct, metrics.r2_q30_pct, metrics.gc_pct,
            metrics.r1_gc_pct, metrics.r2_gc_pct, metrics.clean_bases_gb,
            metrics.base_retention_pct,
        )
    )
    return metrics if meaningful else None


def merge_samples(items: Iterable[SampleMetrics]) -> dict[str, SampleMetrics]:
    merged: dict[str, SampleMetrics] = {}
    for item in items:
        key = item.sample.upper()
        if key not in merged:
            merged[key] = item
        else:
            merged[key].merge(item)
    for item in merged.values():
        item.finalize()
    return merged


def sample_sort_key(sample: SampleMetrics) -> tuple:
    match = re.match(r"(\d+)([A-Za-z].*)?", sample.sample)
    if match:
        return int(match.group(1)), match.group(2) or ""
    return sys.maxsize, sample.sample


def patient_key(sample_name: str) -> str:
    match = re.match(r"(.+?)([DR])$", sample_name, re.I)
    return match.group(1) if match else sample_name


def find_archive_tool() -> Optional[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if os.name == "nt":
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        for root in program_files:
            if root:
                candidates.extend([
                    (str(Path(root) / "WinRAR" / "UnRAR.exe"), "unrar"),
                    (str(Path(root) / "WinRAR" / "WinRAR.exe"), "winrar"),
                    (str(Path(root) / "7-Zip" / "7z.exe"), "7zip"),
                ])
    for name, kind in (("unrar", "unrar"), ("rar", "unrar"), ("7z", "7zip"), ("7za", "7zip")):
        executable = shutil.which(name)
        if executable:
            candidates.append((executable, kind))
    for executable, kind in candidates:
        if Path(executable).exists() or shutil.which(executable):
            return executable, kind
    return None


def extract_archive(path: Path, temp_dirs: list[Path]) -> Path:
    out_dir = Path(tempfile.mkdtemp(prefix="ngs_qc_"))
    temp_dirs.append(out_dir)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as archive:
            archive.extractall(out_dir)
        return out_dir

    tool = find_archive_tool()
    if not tool:
        raise RuntimeError(
            "RAR arşivini açmak için bilgisayarda WinRAR, UnRAR veya 7-Zip bulunamadı. "
            "Arşivi önce dışarı çıkarıp klasörü seçebilirsiniz."
        )
    executable, kind = tool
    if kind == "unrar":
        command = [executable, "x", "-o+", "-y", str(path), str(out_dir) + os.sep]
    elif kind == "winrar":
        command = [executable, "x", "-ibck", "-o+", "-y", str(path), str(out_dir) + os.sep]
    else:
        command = [executable, "x", "-y", f"-o{out_dir}", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Arşiv açılamadı.\n\n{result.stderr or result.stdout}")
    return out_dir


class NGSQCApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1380x860")
        self.minsize(1080, 700)
        self.samples: dict[str, SampleMetrics] = {}
        self.selected_paths: list[Path] = []
        self.temp_dirs: list[Path] = []
        self.status_var = tk.StringVar(value="Dosya, klasör veya arşiv seçin.")
        self.report_type_var = tk.StringVar(value="AUTO")
        self.active_type_var = tk.StringVar(value="Etkin tür: DNA (otomatik varsayılan)")
        self.block_detail_var = tk.StringVar(value="")
        self.tumor_pct_var = tk.StringVar(value="")
        self.tmb_var = tk.StringVar(value="")
        self.tmb_high_threshold_var = tk.StringVar(value="10")
        self.msi_pct_var = tk.StringVar(value="")
        self.msi_mss_limit_var = tk.StringVar(value="0")
        self.msi_high_limit_var = tk.StringVar(value="40")
        self.hrd_score_var = tk.StringVar(value="")
        self.dna_positive_vars = {gene: tk.BooleanVar(value=False) for gene in DNA_NEGATIVE_GENES}
        self.standard_sections: list[tuple[str, str]] = []
        self._build_ui()
        self._bind_standard_updates()
        self.refresh_standard_sections()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass

        toolbar = ttk.Frame(self, padding=(10, 10, 10, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Dosya / Arşiv Seç", command=self.choose_files).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Klasör Seç", command=self.choose_folder).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Yeniden Analiz Et", command=self.analyze).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Temizle", command=self.clear_all).pack(side="left", padx=6)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(toolbar, text="Seçili QC Cümlesini Kopyala", command=self.copy_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Tüm QC Cümlelerini Kopyala", command=self.copy_all).pack(side="left", padx=6)
        ttk.Button(toolbar, text="CSV Dışa Aktar", command=self.export_csv).pack(side="left", padx=6)

        info = ttk.Label(
            self,
            text=(
                "QC dosyaları solda analiz edilir. Standart Metinler sekmesinde seçili protokole ait "
                "tüm rapor metni tek parça olarak hazırlanır ve panoya kopyalanabilir."
            ),
            padding=(12, 0, 12, 8),
        )
        info.pack(fill="x")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=3)
        paned.add(right, weight=4)

        columns = ("sample", "type", "pairs", "q30", "gc", "clean", "retention", "status")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        headings = {
            "sample": "Protokol",
            "type": "Tür",
            "pairs": "Okuma çifti (M)",
            "q30": "Q30 (%)",
            "gc": "GC (%)",
            "clean": "Temiz veri (Gb)",
            "retention": "Baz korunumu (%)",
            "status": "Durum",
        }
        widths = {"sample": 90, "type": 60, "pairs": 115, "q30": 80, "gc": 75, "clean": 105, "retention": 125, "status": 180}
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self.sort_tree(c, False))
            self.tree.column(col, width=widths[col], anchor="center" if col != "status" else "w")

        yscroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Double-1>", lambda _event: self.copy_selected())

        notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True)

        qc_tab = ttk.Frame(notebook, padding=8)
        standard_tab = ttk.Frame(notebook, padding=8)
        notebook.add(qc_tab, text="QC Cümlesi")
        notebook.add(standard_tab, text="Standart Metinler")

        ttk.Label(qc_tab, text="Rapora eklenecek QC metni", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        qc_text_frame = ttk.Frame(qc_tab)
        qc_text_frame.pack(fill="both", expand=True)
        self.output = tk.Text(
            qc_text_frame,
            wrap="word",
            font=("Segoe UI", 11),
            padx=12,
            pady=12,
            undo=False,
        )
        output_scroll = ttk.Scrollbar(qc_text_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scroll.pack(side="right", fill="y")

        self._build_standard_tab(standard_tab)

        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4)).pack(fill="x", side="bottom")

    def _build_standard_tab(self, parent: ttk.Frame) -> None:
        type_frame = ttk.LabelFrame(parent, text="Metin türü", padding=8)
        type_frame.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(type_frame, text="Seçili örnekten otomatik", value="AUTO", variable=self.report_type_var, command=self.refresh_standard_sections).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(type_frame, text="DNA", value="DNA", variable=self.report_type_var, command=self.refresh_standard_sections).grid(row=0, column=1, sticky="w", padx=(14, 0))
        ttk.Radiobutton(type_frame, text="RNA", value="RNA", variable=self.report_type_var, command=self.refresh_standard_sections).grid(row=0, column=2, sticky="w", padx=(14, 0))
        ttk.Label(type_frame, textvariable=self.active_type_var, font=("Segoe UI", 9, "bold")).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        sample_frame = ttk.LabelFrame(parent, text="Örnek / blok bilgisi", padding=8)
        sample_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(sample_frame, text="SDÜ Tıp Fakültesi sonrası blok bilgisi:").grid(row=0, column=0, sticky="w")
        ttk.Entry(sample_frame, textvariable=self.block_detail_var, width=34).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(sample_frame, text="Tümör oranı (%):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(sample_frame, textvariable=self.tumor_pct_var, width=12).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        sample_frame.columnconfigure(1, weight=1)

        biomarker_frame = ttk.LabelFrame(parent, text="DNA biyobelirteç alanları", padding=8)
        biomarker_frame.pack(fill="x", pady=(0, 8))
        self.dna_widgets: list[tk.Widget] = []

        labels_entries = [
            ("TMB (mut/Mb):", self.tmb_var, 0, 0),
            ("TMB-H eşiği:", self.tmb_high_threshold_var, 0, 2),
            ("MSI Percentage (%):", self.msi_pct_var, 1, 0),
            ("MSS üst sınırı:", self.msi_mss_limit_var, 1, 2),
            ("MSI-H alt sınırı:", self.msi_high_limit_var, 2, 0),
            ("HRD Score:", self.hrd_score_var, 2, 2),
        ]
        for label_text, variable, row, column in labels_entries:
            label = ttk.Label(biomarker_frame, text=label_text)
            entry = ttk.Entry(biomarker_frame, textvariable=variable, width=12)
            label.grid(row=row, column=column, sticky="w", pady=2)
            entry.grid(row=row, column=column + 1, sticky="w", padx=(8, 18), pady=2)
            self.dna_widgets.extend([label, entry])
        ttk.Label(
            biomarker_frame,
            text="Varsayılan sınıflama: MSS %0; MSI-L >%0–%40; MSI-H >%40; TMB-H ≥10 mut/Mb. Eşikler değiştirilebilir.",
            foreground="#555555",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(5, 0))

        finding_frame = ttk.LabelFrame(parent, text="Pozitif mutasyon / füzyon seçimi", padding=8)
        finding_frame.pack(fill="x", pady=(0, 8))

        self.dna_positive_frame = ttk.Frame(finding_frame)
        self.dna_positive_frame.pack(fill="x")
        ttk.Label(self.dna_positive_frame, text="DNA — mutasyon saptanan gen(ler):").pack(side="left", padx=(0, 8))
        self.dna_positive_widgets: list[tk.Widget] = []
        for gene in DNA_NEGATIVE_GENES:
            widget = ttk.Checkbutton(
                self.dna_positive_frame,
                text=gene,
                variable=self.dna_positive_vars[gene],
                command=self.refresh_standard_sections,
            )
            widget.pack(side="left", padx=4)
            self.dna_positive_widgets.append(widget)
        ttk.Button(
            self.dna_positive_frame,
            text="DNA seçimlerini temizle",
            command=self.clear_dna_positive_selection,
        ).pack(side="right", padx=(12, 0))

        self.rna_positive_frame = ttk.Frame(finding_frame)
        self.rna_positive_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(
            self.rna_positive_frame,
            text="RNA — füzyon saptanan gen(ler); birden fazla gen doğrudan tıklanabilir:",
        ).pack(anchor="w")
        rna_list_frame = ttk.Frame(self.rna_positive_frame)
        rna_list_frame.pack(fill="x", pady=(4, 0))
        self.rna_positive_list = tk.Listbox(
            rna_list_frame,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            height=5,
            font=("Segoe UI", 9),
        )
        for gene in RNA_PANEL_GENES:
            self.rna_positive_list.insert("end", gene)
        rna_scroll = ttk.Scrollbar(rna_list_frame, orient="vertical", command=self.rna_positive_list.yview)
        self.rna_positive_list.configure(yscrollcommand=rna_scroll.set)
        self.rna_positive_list.pack(side="left", fill="x", expand=True)
        rna_scroll.pack(side="right", fill="y")
        self.rna_positive_list.bind("<<ListboxSelect>>", lambda _event: self.refresh_standard_sections())
        ttk.Button(
            self.rna_positive_frame,
            text="RNA seçimlerini temizle",
            command=self.clear_rna_positive_selection,
        ).pack(anchor="e", pady=(4, 0))

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill="x", pady=(0, 6))
        ttk.Button(
            action_frame,
            text="Tüm Standart Metni Kopyala",
            command=self.copy_standard_all,
        ).pack(side="left")
        ttk.Label(
            action_frame,
            text="Üstteki teknik bilgi bloğu rapora aktarılmayacaktır.",
            foreground="#555555",
        ).pack(side="left", padx=(12, 0))

        preview_frame = ttk.Frame(parent)
        preview_frame.pack(fill="both", expand=True)
        ttk.Label(
            preview_frame,
            text="Tüm standart metin",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(4, 4))
        preview_text_frame = ttk.Frame(preview_frame)
        preview_text_frame.pack(fill="both", expand=True)
        self.standard_output = tk.Text(
            preview_text_frame,
            wrap="word",
            font=("Segoe UI", 10),
            padx=10,
            pady=10,
        )
        standard_scroll = ttk.Scrollbar(
            preview_text_frame,
            orient="vertical",
            command=self.standard_output.yview,
        )
        self.standard_output.configure(yscrollcommand=standard_scroll.set)
        self.standard_output.pack(side="left", fill="both", expand=True)
        standard_scroll.pack(side="right", fill="y")

    def _bind_standard_updates(self) -> None:
        for variable in (
            self.block_detail_var,
            self.tumor_pct_var,
            self.tmb_var,
            self.tmb_high_threshold_var,
            self.msi_pct_var,
            self.msi_mss_limit_var,
            self.msi_high_limit_var,
            self.hrd_score_var,
        ):
            variable.trace_add("write", lambda *_args: self.refresh_standard_sections())

    @staticmethod
    def _entry_number(value: str) -> Optional[float]:
        value = value.strip()
        if not value:
            return None
        try:
            return float(value.replace(" ", "").replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _display_entry_number(value: str) -> str:
        parsed = NGSQCApp._entry_number(value)
        if parsed is None:
            return value.strip()
        if parsed.is_integer():
            return str(int(parsed))
        return (f"{parsed:.2f}".rstrip("0").rstrip(".")).replace(".", ",")

    def clear_dna_positive_selection(self) -> None:
        for variable in self.dna_positive_vars.values():
            variable.set(False)
        self.refresh_standard_sections()

    def clear_rna_positive_selection(self) -> None:
        if hasattr(self, "rna_positive_list"):
            self.rna_positive_list.selection_clear(0, "end")
        self.refresh_standard_sections()

    def selected_dna_positive_genes(self) -> list[str]:
        return [gene for gene in DNA_NEGATIVE_GENES if self.dna_positive_vars[gene].get()]

    def selected_rna_positive_genes(self) -> list[str]:
        if not hasattr(self, "rna_positive_list"):
            return []
        return [RNA_PANEL_GENES[index] for index in self.rna_positive_list.curselection()]

    @staticmethod
    def _join_gene_names(genes: list[str], display_hyphen: bool = False) -> str:
        names = [re.sub(r"^(FGFR)([123])$", r"\1-\2", gene) if display_hyphen else gene for gene in genes]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} ve {names[1]}"
        return ", ".join(names[:-1]) + f" ve {names[-1]}"

    def dna_results_block(self) -> str:
        selected = set(self.selected_dna_positive_genes())
        lines: list[str] = []
        lines.extend(f"- {gene}:" for gene in DNA_NEGATIVE_GENES if gene in selected)
        lines.extend(
            DNA_NEGATIVE_TEMPLATE.format(gene=gene)
            for gene in DNA_NEGATIVE_GENES
            if gene not in selected
        )
        lines.extend([
            self.msi_result_text(),
            self.tmb_result_text(),
            self.hrd_result_text(),
            OTHER_GENES_NEGATIVE_TEXT,
            METHOD_DETAILS_TEXT,
        ])
        return "\n".join(lines)

    def rna_results_block(self) -> str:
        selected = set(self.selected_rna_positive_genes())
        lines: list[str] = []
        lines.extend(f"- {gene}:" for gene in RNA_PANEL_GENES if gene in selected)

        general_remaining = [gene for gene in RNA_GENERAL_NEGATIVE_GENES if gene not in selected]
        if general_remaining:
            lines.append(
                f"- {', '.join(general_remaining)} genlerinde füzyon izlenmemiştir."
            )

        if "ROS1" not in selected:
            lines.append("- ROS1 geninde füzyon izlenmemiştir.")

        ntrk_remaining = [gene for gene in ("NTRK1", "NTRK2", "NTRK3") if gene not in selected]
        if ntrk_remaining:
            gene_text = self._join_gene_names(ntrk_remaining)
            suffix = "geninde" if len(ntrk_remaining) == 1 else "genlerinde"
            lines.append(f"- {gene_text} {suffix} füzyon izlenmemiştir.")

        fgfr_remaining = [gene for gene in ("FGFR1", "FGFR2", "FGFR3") if gene not in selected]
        if fgfr_remaining:
            gene_text = self._join_gene_names(fgfr_remaining, display_hyphen=True)
            suffix = "geninde" if len(fgfr_remaining) == 1 else "genlerinde"
            lines.append(f"- {gene_text} {suffix} füzyon izlenmemiştir.")

        if "ALK" not in selected:
            lines.append("- ALK geninde füzyon izlenmemiştir.")

        lines.extend([OTHER_GENES_NEGATIVE_TEXT, METHOD_DETAILS_TEXT])
        return "\n".join(lines)

    def active_report_type(self) -> str:
        forced = self.report_type_var.get()
        if forced in {"DNA", "RNA"}:
            return forced
        selected = self.tree.selection() if hasattr(self, "tree") else ()
        if selected:
            metrics = self.samples.get(selected[0])
            if metrics and metrics.sample_type in {"DNA", "RNA"}:
                return metrics.sample_type
        return "DNA"

    def paraffin_text(self) -> str:
        detail = self.block_detail_var.get().strip()
        first = "SDÜ Tıp Fakültesi"
        if detail:
            first += " " + detail
        first += " parafin bloktan çalışılmıştır."
        tumor = self._display_entry_number(self.tumor_pct_var.get())
        second = f"Blokta tümör oranı %{tumor}." if tumor else "Blokta tümör oranı %"
        return first + " " + second

    def msi_result_text(self) -> str:
        raw = self.msi_pct_var.get().strip()
        value = self._entry_number(raw)
        if not raw:
            return "- MSI durumu:"
        if value is None:
            return "- MSI durumu: [MSI Percentage sayısal olarak girilmelidir.]"
        mss_limit = self._entry_number(self.msi_mss_limit_var.get())
        high_limit = self._entry_number(self.msi_high_limit_var.get())
        mss_limit = 0.0 if mss_limit is None else mss_limit
        high_limit = 40.0 if high_limit is None else high_limit
        shown = self._display_entry_number(raw)
        if value <= mss_limit:
            status = "Mikrosatellit Stabil (MS-Stable/MSS)"
        elif value <= high_limit:
            status = "Mikrosatellit İnstabilite Low (MSI-Low)"
        else:
            status = "Mikrosatellit İnstabilite High (MSI-High)"
        return f"- MSI durumu: {status}. MSI instabilite tespit edilen bölgelerin yüzdesi: %{shown}."

    def tmb_result_text(self) -> str:
        raw = self.tmb_var.get().strip()
        value = self._entry_number(raw)
        if not raw:
            return "- Tümör Mutasyon Yükü — TMB: TMB değeri  mut/Mb olarak hesaplanmıştır."
        if value is None:
            return "- Tümör Mutasyon Yükü — TMB: [TMB değeri sayısal olarak girilmelidir.]"
        threshold = self._entry_number(self.tmb_high_threshold_var.get())
        threshold = 10.0 if threshold is None else threshold
        category = "TMB-H" if value >= threshold else "TMB-L"
        shown = self._display_entry_number(raw)
        return f"- Tümör Mutasyon Yükü — TMB: TMB değeri {shown} mut/Mb olarak hesaplanmıştır ({category})."

    def hrd_result_text(self) -> str:
        raw = self.hrd_score_var.get().strip()
        if not raw:
            return "- Homolog Rekombinasyon Yetersizliği — HRD:"
        value = self._entry_number(raw)
        if value is None:
            return "- Homolog Rekombinasyon Yetersizliği — HRD: [HRD Score sayısal olarak girilmelidir.]"
        shown = self._display_entry_number(raw)
        return f"- Homolog Rekombinasyon Yetersizliği — HRD: HRD skoru {shown} olarak hesaplanmıştır."

    def build_standard_sections(self) -> list[tuple[str, str]]:
        report_type = self.active_report_type()
        sections: list[tuple[str, str]] = [("Parafin blok ve tümör oranı", self.paraffin_text())]
        if report_type == "DNA":
            sections.extend([
                ("DNA paneli — yöntem", DNA_METHOD_TEXT),
                ("MSI — değerlendirme yöntemi", MSI_METHOD_TEXT),
                ("DNA paneli — incelenen genler", DNA_PANEL_TEXT),
                ("DNA — sonuç bloğu", self.dna_results_block()),
            ])
        else:
            sections.extend([
                ("RNA paneli — yöntem", RNA_METHOD_TEXT),
                ("RNA paneli — incelenen genler", RNA_PANEL_TEXT),
                ("RNA — sonuç bloğu", self.rna_results_block()),
            ])
        return sections

    def selected_metrics(self) -> Optional[SampleMetrics]:
        if not hasattr(self, "tree"):
            return None
        selected = self.tree.selection()
        if not selected:
            return None
        return self.samples.get(selected[0])

    @staticmethod
    def _work_codes_from_files(source_files: set[str]) -> list[str]:
        codes: set[str] = set()
        for source in source_files:
            name = Path(source).name
            match = re.search(r"_L\d+_(\d+)(?:[_.]|$)", name, re.I)
            if match:
                codes.add(match.group(1))
        return sorted(codes, key=lambda value: int(value) if value.isdigit() else value)

    def technical_header_text(self) -> str:
        metrics = self.selected_metrics()
        report_type = self.active_report_type()
        if metrics is None:
            return "\n".join([
                "[RAPORA GİRMEYECEK TEKNİK BİLGİ]",
                "Protokol: —",
                f"Çalışma türü: {report_type}",
                "Örnek kodu: —",
                "Kaynak QC dosyaları: —",
            ])

        protocol = patient_key(metrics.sample)
        file_names = sorted(Path(source).name for source in metrics.source_files)
        work_codes = self._work_codes_from_files(metrics.source_files)
        missing = metrics.missing_required()
        qc_status = "Tam" if not missing else "Eksik: " + ", ".join(missing)
        lines = [
            "[RAPORA GİRMEYECEK TEKNİK BİLGİ]",
            f"Protokol: {protocol}",
            f"Çalışma türü: {metrics.sample_type or report_type}",
            f"Örnek kodu: {metrics.sample}",
        ]
        if work_codes:
            lines.append(f"Çalışma/dosya kodu: {', '.join(work_codes)}")
        lines.append(f"QC durumu: {qc_status}")
        lines.append(f"Kaynak QC dosya sayısı: {len(file_names)}")
        if file_names:
            lines.append("Kaynak QC dosyaları: " + " | ".join(file_names))
        else:
            lines.append("Kaynak QC dosyaları: —")
        return "\n".join(lines)

    def full_standard_text(self) -> str:
        body = "\n\n".join(section_text for _title, section_text in self.standard_sections)
        return f"{self.technical_header_text()}\n-------------\n\n{body}".strip()

    def refresh_standard_sections(self) -> None:
        if not hasattr(self, "standard_output"):
            return
        report_type = self.active_report_type()
        selected = self.tree.selection() if hasattr(self, "tree") else ()
        protocol = patient_key(selected[0]) if selected else "—"
        mode = self.report_type_var.get()
        suffix = "seçili örnekten" if mode == "AUTO" and selected else ("otomatik varsayılan" if mode == "AUTO" else "elle seçildi")
        self.active_type_var.set(f"Etkin tür: {report_type} | Protokol: {protocol} ({suffix})")

        dna_state = "normal" if report_type == "DNA" else "disabled"
        for widget in self.dna_widgets + getattr(self, "dna_positive_widgets", []):
            try:
                widget.configure(state=dna_state)
            except tk.TclError:
                pass
        if hasattr(self, "rna_positive_list"):
            self.rna_positive_list.configure(state="normal" if report_type == "RNA" else "disabled")

        self.standard_sections = self.build_standard_sections()
        self.standard_output.delete("1.0", "end")
        self.standard_output.insert("1.0", self.full_standard_text())

    def copy_standard_all(self) -> None:
        self.copy_text(self.full_standard_text())

    def choose_files(self) -> None:
        filenames = filedialog.askopenfilenames(
            title="QC raporlarını veya arşivi seçin",
            filetypes=[
                ("NGS QC dosyaları", "*.html *.htm *.txt *.log *.report *.csv *.tsv *.zip *.rar"),
                ("Arşivler", "*.zip *.rar"),
                ("Tüm dosyalar", "*.*"),
            ],
        )
        if filenames:
            self.selected_paths.extend(Path(name) for name in filenames)
            self.selected_paths = list(dict.fromkeys(self.selected_paths))
            self.analyze()

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="QC raporlarının bulunduğu klasörü seçin")
        if folder:
            self.selected_paths.append(Path(folder))
            self.selected_paths = list(dict.fromkeys(self.selected_paths))
            self.analyze()

    def collect_files(self) -> tuple[list[Path], list[str]]:
        files: list[Path] = []
        warnings: list[str] = []
        for selected in self.selected_paths:
            if not selected.exists():
                warnings.append(f"Bulunamadı: {selected}")
                continue
            if selected.is_dir():
                files.extend(p for p in selected.rglob("*") if is_candidate_report(p))
                continue
            suffix = selected.suffix.lower()
            if suffix in ARCHIVE_EXTENSIONS:
                try:
                    extracted = extract_archive(selected, self.temp_dirs)
                    files.extend(p for p in extracted.rglob("*") if is_candidate_report(p))
                except Exception as exc:
                    warnings.append(f"{selected.name}: {exc}")
            elif is_candidate_report(selected):
                files.append(selected)
            elif suffix in IGNORED_LARGE_EXTENSIONS or selected.name.lower().endswith((".fastq.gz", ".fq.gz")):
                warnings.append(f"Ham dizi dosyası atlandı: {selected.name}")
            else:
                warnings.append(f"Desteklenmeyen dosya atlandı: {selected.name}")
        return list(dict.fromkeys(files)), warnings

    def analyze(self) -> None:
        if not self.selected_paths:
            messagebox.showinfo(APP_TITLE, "Önce dosya, klasör veya arşiv seçin.")
            return
        try:
            files, warnings = self.collect_files()
            parsed: list[SampleMetrics] = []
            unparsed: list[str] = []
            for path in files:
                try:
                    if path.suffix.lower() in {".csv", ".tsv"}:
                        csv_rows = parse_csv_file(path)
                        if csv_rows:
                            parsed.extend(csv_rows)
                        else:
                            unparsed.append(path.name)
                    else:
                        item = parse_report_file(path)
                        if item:
                            parsed.append(item)
                        else:
                            unparsed.append(path.name)
                except Exception as exc:
                    warnings.append(f"{path.name}: {exc}")

            self.samples = merge_samples(parsed)
            self.refresh_tree()

            complete = sum(not item.missing_required() for item in self.samples.values())
            status = f"v1.5 | {len(files)} dosya incelendi; {len(self.samples)} örnek bulundu; {complete} örnek rapor cümlesi için tam."
            if unparsed:
                status += f" Okunamayan/uygun metrik içermeyen dosya: {len(unparsed)}."
            if warnings:
                status += f" Uyarı: {len(warnings)}."
            self.status_var.set(status)
            if warnings:
                messagebox.showwarning(APP_TITLE, "\n\n".join(warnings[:12]))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Analiz sırasında hata oluştu:\n\n{exc}")

    def refresh_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for metrics in sorted(self.samples.values(), key=sample_sort_key):
            missing = metrics.missing_required()
            status = "Tam" if not missing else "Eksik: " + ", ".join(missing)
            values = (
                patient_key(metrics.sample),
                metrics.sample_type or "?",
                tr_num(metrics.total_reads_m, 2) if metrics.total_reads_m is not None else "—",
                tr_num(metrics.q30_pct, 2) if metrics.q30_pct is not None else "—",
                tr_num(metrics.gc_pct, 2) if metrics.gc_pct is not None else "—",
                tr_num(metrics.clean_bases_gb, 2) if metrics.clean_bases_gb is not None else "—",
                tr_num(metrics.base_retention_pct, 2) if metrics.base_retention_pct is not None else "—",
                status,
            )
            self.tree.insert("", "end", iid=metrics.sample, values=values)
        self.output.delete("1.0", "end")
        if self.samples:
            first = sorted(self.samples.values(), key=sample_sort_key)[0].sample
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.on_tree_select()
        else:
            self.refresh_standard_sections()

    def on_tree_select(self, _event=None) -> None:
        selected = self.tree.selection()
        self.output.delete("1.0", "end")
        if selected:
            chunks: list[str] = []
            for sample_name in selected:
                metrics = self.samples.get(sample_name)
                if not metrics:
                    continue
                chunks.append(f"Protokol {patient_key(metrics.sample)} – {metrics.sample_type or '?'}\n{metrics.report_sentence()}")
                missing = metrics.missing_required()
                if missing:
                    chunks.append("[Eksik metrik: " + ", ".join(missing) + "]")
            self.output.insert("1.0", "\n\n".join(chunks))
        self.refresh_standard_sections()

    def copy_text(self, text: str) -> None:
        if not text.strip():
            messagebox.showinfo(APP_TITLE, "Kopyalanacak metin bulunmuyor.")
            return
        self.clipboard_clear()
        self.clipboard_append(text.strip())
        self.update()
        self.status_var.set("Metin panoya kopyalandı.")

    def copy_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "Önce bir örnek seçin.")
            return
        text = "\n\n".join(self.samples[name].report_sentence() for name in selected if name in self.samples)
        self.copy_text(text)

    def all_report_text(self) -> str:
        grouped: dict[str, list[SampleMetrics]] = {}
        for metrics in sorted(self.samples.values(), key=sample_sort_key):
            grouped.setdefault(patient_key(metrics.sample), []).append(metrics)
        chunks: list[str] = []
        for patient, metrics_list in grouped.items():
            chunks.append(f"Hasta {patient}")
            for metrics in sorted(metrics_list, key=lambda x: (x.sample_type != "DNA", x.sample)):
                chunks.append(metrics.report_sentence())
            chunks.append("")
        return "\n".join(chunks).strip()

    def copy_all(self) -> None:
        self.copy_text(self.all_report_text())

    def export_csv(self) -> None:
        if not self.samples:
            messagebox.showinfo(APP_TITLE, "Dışa aktarılacak sonuç bulunmuyor.")
            return
        filename = filedialog.asksaveasfilename(
            title="QC sonuçlarını kaydet",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="ngs_qc_rapor_sonuclari.csv",
        )
        if not filename:
            return
        with open(filename, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow([
                "Örnek", "Tür", "Okuma çifti (M)", "Toplam bireysel okuma (M)",
                "Q30 (%)", "GC (%)", "Temiz veri (Gb)", "Baz korunumu (%)",
                "Durum", "Rapor cümlesi", "Kaynak dosyalar",
            ])
            for metrics in sorted(self.samples.values(), key=sample_sort_key):
                missing = metrics.missing_required()
                writer.writerow([
                    metrics.sample,
                    metrics.sample_type,
                    tr_num(metrics.total_reads_m, 2) if metrics.total_reads_m is not None else "",
                    tr_num(metrics.total_reads_m * 2, 2) if metrics.total_reads_m is not None else "",
                    tr_num(metrics.q30_pct, 2) if metrics.q30_pct is not None else "",
                    tr_num(metrics.gc_pct, 2) if metrics.gc_pct is not None else "",
                    tr_num(metrics.clean_bases_gb, 2) if metrics.clean_bases_gb is not None else "",
                    tr_num(metrics.base_retention_pct, 2) if metrics.base_retention_pct is not None else "",
                    "Tam" if not missing else "Eksik: " + ", ".join(missing),
                    metrics.report_sentence(),
                    " | ".join(sorted(metrics.source_files)),
                ])
        self.status_var.set(f"CSV kaydedildi: {filename}")

    def sort_tree(self, column: str, reverse: bool) -> None:
        rows = [(self.tree.set(item, column), item) for item in self.tree.get_children("")]

        def key(value_item: tuple[str, str]):
            value = value_item[0]
            try:
                return 0, parse_number(value)
            except (ValueError, TypeError):
                return 1, value.casefold()

        rows.sort(key=key, reverse=reverse)
        for index, (_, item) in enumerate(rows):
            self.tree.move(item, "", index)
        self.tree.heading(column, command=lambda: self.sort_tree(column, not reverse))

    def clear_all(self) -> None:
        self.samples.clear()
        self.selected_paths.clear()
        self.refresh_tree()
        self.status_var.set("QC sonuçları temizlendi. Standart metin alanları kullanılmaya devam edebilir.")

    def on_close(self) -> None:
        for directory in self.temp_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        self.destroy()


if __name__ == "__main__":
    app = NGSQCApp()
    app.mainloop()
