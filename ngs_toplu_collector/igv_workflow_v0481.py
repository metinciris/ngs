"""Request-scoped IGV persistence and evidence exchange.

Images, review evidence and clinical approval intentionally have separate states.
This module neither writes GenNext Selection nor changes a manual report decision.
All added database structures are additive; existing images are never moved/deleted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REVIEW_SCHEMAS = {"ngs_igv_review_v1", "ngs_igv_review_v2"}
ZOOMS = ("Z1", "Z2", "Z3", "Z4")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value):
    return str(value or "").strip()


def _obj(value):
    return value if isinstance(value, dict) else {}


def _decoded(value):
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def canonical_build(value):
    raw = _text(value)
    aliases = {"hg38": "hg38", "grch38": "hg38", "hg19": "hg19", "grch37": "hg19"}
    return aliases.get(raw.casefold(), raw)


def _revision(value):
    if isinstance(value, bool):
        raise ValueError("IGV request_revision pozitif tam sayı olmalıdır.")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("IGV request_revision pozitif tam sayı olmalıdır.")
    if n < 1 or str(value).strip() not in (str(n), str(n) + ".0") or isinstance(value, float) and value != n:
        raise ValueError("IGV request_revision pozitif tam sayı olmalıdır.")
    return n


def request_identity(request, root_build="", allow_legacy=False):
    """Immutable identity includes original alleles/key and the requested region."""
    req = _obj(request)
    coord = _obj(req.get("coordinates"))
    rk, vk = _text(req.get("record_key")), _text(req.get("variant_key"))
    build = canonical_build(coord.get("genome_build") or req.get("genome_build") or root_build)
    region = _text(req.get("region") or coord.get("region") or req.get("requested_region"))
    if not rk or not vk:
        raise ValueError("IGV isteğinde record_key ve variant_key zorunludur.")
    if coord.get("region") and req.get("region") and _text(coord["region"]) != _text(req["region"]):
        raise ValueError("IGV coordinates.region ile region farklı.")
    if root_build and build and canonical_build(root_build) != build:
        raise ValueError("IGV genom sürümü kök belgeyle uyuşmuyor.")
    if not build and not allow_legacy:
        raise ValueError("IGV isteğinde genome_build doğrulanmamış.")
    parts = vk.split("|")
    if parts and parts[0].upper() in ("SNV", "INDEL", "VARIANT"):
        parts = parts[1:]
    if len(parts) == 4 and parts[1].isdigit():
        chrom, pos, ref, alt = parts
        norm_chr = lambda s: _text(s).lower().removeprefix("chr")
        if coord.get("chrom") and norm_chr(coord["chrom"]) != norm_chr(chrom):
            raise ValueError("IGV variant_key kromozomu coordinates ile uyuşmuyor.")
        for key, expected in (("position", int(pos)), ("ref", ref), ("alt", alt)):
            if coord.get(key) not in (None, "") and str(coord[key]) != str(expected):
                raise ValueError("IGV variant_key ile coordinates." + key + " uyuşmuyor.")
        if region:
            m = re.fullmatch(r"([^:]+):(\d+)-(\d+)", region)
            if not m or norm_chr(m[1]) != norm_chr(chrom) or not int(m[2]) <= int(pos) <= int(m[3]):
                raise ValueError("IGV bölgesi kaynak varyant lokusunu kapsamıyor.")
    rid = _text(req.get("request_id"))
    revision = req.get("request_revision")
    origin = _text(req.get("request_identity_origin")) or "provided_request_identity"
    if not rid and allow_legacy:
        rid = "LEGACY_" + _hash([rk, vk, build, region])[:24]
        revision = 1
        origin = "reconstructed_from_legacy_request_fields"
    if not rid:
        raise ValueError("IGV request_id zorunludur.")
    revision = _revision(revision)
    return {"request_id": rid, "request_revision": revision, "record_key": rk,
            "variant_key": vk, "genome_build": build, "region": region,
            "parent_request_id": _text(req.get("parent_request_id")),
            "request_identity_origin": origin}


def _identity_fields(identity):
    return {key: identity.get(key) for key in ("request_id", "request_revision", "record_key", "variant_key", "genome_build", "region")}


def ensure_igv_workflow_schema(db):
    if getattr(db, "_igv_workflow_schema_ready_v0481", False):
        return
    db.ensure_ai_review_schema()
    cols = {r["name"] for r in db.cx.execute("PRAGMA table_info(igv_queue)").fetchall()}
    for col, ddl in (("request_id", "TEXT DEFAULT ''"), ("request_revision", "INTEGER DEFAULT 0"),
                     ("parent_request_id", "TEXT DEFAULT ''"), ("source_identity_json", "TEXT DEFAULT ''"),
                     ("pileup_json", "TEXT DEFAULT ''"), ("coverage_audit_json", "TEXT DEFAULT ''"),
                     ("request_payload_json", "TEXT DEFAULT ''"), ("genome_build", "TEXT DEFAULT ''"),
                     ("manual_decision_request_id", "TEXT DEFAULT ''")):
        if col not in cols:
            db.cx.execute("ALTER TABLE igv_queue ADD COLUMN " + col + " " + ddl)
    db.cx.executescript("""
        CREATE TABLE IF NOT EXISTS igv_request_registry_v0481(
          request_id TEXT PRIMARY KEY, request_revision INTEGER NOT NULL, record_key TEXT NOT NULL,
          variant_key TEXT NOT NULL, genome_build TEXT, region TEXT, parent_request_id TEXT,
          identity_origin TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_igv_request_record_v0481 ON igv_request_registry_v0481(record_key,variant_key);
        CREATE TABLE IF NOT EXISTS igv_request_history_v0481(
          id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER, request_id TEXT, request_revision INTEGER,
          superseded_by_request_id TEXT, queue_snapshot_json TEXT NOT NULL, archived_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_import_documents_v0481(
          content_sha256 TEXT PRIMARY KEY, session_id INTEGER, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS igv_review_results_v0481(
          id INTEGER PRIMARY KEY AUTOINCREMENT, result_key TEXT UNIQUE NOT NULL,
          request_id TEXT NOT NULL, request_revision INTEGER NOT NULL, record_key TEXT NOT NULL,
          variant_key TEXT NOT NULL, genome_build TEXT NOT NULL, queue_id INTEGER,
          evidence TEXT NOT NULL, confidence TEXT NOT NULL, manual_review INTEGER NOT NULL,
          capture_status TEXT NOT NULL, clinical_report_release INTEGER NOT NULL DEFAULT 0,
          linkage_json TEXT NOT NULL, source_metrics_json TEXT NOT NULL, measured_metrics_json TEXT NOT NULL,
          raw_json TEXT NOT NULL, source_bundle TEXT, source_sha256 TEXT, imported_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_igv_result_request_v0481 ON igv_review_results_v0481(request_id,request_revision);
        CREATE TABLE IF NOT EXISTS igv_unmatched_reviews_v0481(
          result_key TEXT PRIMARY KEY, reason TEXT NOT NULL, raw_json TEXT NOT NULL, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS igv_bundle_identity_v0481(
          bundle_name TEXT NOT NULL, legacy_id TEXT NOT NULL, request_id TEXT NOT NULL,
          identity_json TEXT NOT NULL, PRIMARY KEY(bundle_name,legacy_id,request_id));
        CREATE TABLE IF NOT EXISTS ai_cnv_reviews_v0481(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, record_key TEXT NOT NULL,
          cnv_key TEXT NOT NULL, raw_json TEXT NOT NULL, imported_at TEXT NOT NULL);
    """)
    db.cx.commit()
    db._igv_workflow_schema_ready_v0481 = True


def _registered(db, rid):
    row = db.cx.execute("SELECT * FROM igv_request_registry_v0481 WHERE request_id=?", (rid,)).fetchone()
    return dict(row) if row else {}


def _request_from_queue(row):
    payload = _decoded(row.get("request_payload_json"))
    return {**payload, **{k: row.get(k) or payload.get(k) for k in ("request_id", "request_revision", "record_key", "variant_key", "genome_build", "region", "parent_request_id")}}


def _original_queue_request(db, row):
    payload = _decoded(row.get("request_payload_json"))
    if payload:
        return payload
    source = db.cx.execute("SELECT raw_json FROM ai_review_sessions WHERE id=?", (row.get("source_session_id") or 0,)).fetchone()
    original = _decoded(source["raw_json"]) if source else {}
    candidates = [r for r in original.get("igv_requests") or [] if isinstance(r, dict)
                  and r.get("record_key") == row.get("record_key") and r.get("variant_key") == row.get("variant_key")
                  and r.get("request_id") == row.get("request_id")]
    if len(candidates) == 1:
        return {**candidates[0], "genome_build": _obj(candidates[0].get("coordinates")).get("genome_build") or original.get("genome_build") or ""}
    return {}


def _legacy_capture_linkage(capture, request, queue_row, origin):
    """Reconstruct only from exact local study/alleles and the original request."""
    if not capture or capture.get("request_id") or capture.get("record_key") != request.get("record_key") or capture.get("variant_key") != request.get("variant_key"):
        return {}
    if int(capture.get("queue_id") or 0) != int(queue_row.get("id") or 0):
        return {}
    coords = _obj(request.get("coordinates"))
    target = _obj(capture.get("target"))
    if not coords or not canonical_build(coords.get("genome_build") or request.get("genome_build")):
        return {}
    normalize_chr = lambda value: _text(value).lower().removeprefix("chr")
    if normalize_chr(target.get("chrom")) != normalize_chr(coords.get("chrom")):
        return {}
    if any(str(target.get(a)) != str(coords.get(b)) for a, b in (("pos", "position"), ("ref", "ref"), ("alt", "alt"))):
        return {}
    if _text(capture.get("requested_region")) != _text(request.get("region") or coords.get("region")):
        return {}
    return {"origin": origin, "legacy_id": capture.get("id"), "explicit_capture_revision_verified": False,
            "unique_match_count": 1, "request_identity": _identity_fields(request_identity(request)),
            "matching_fields": ["local queue id", "record_key", "original variant_key", "chrom/position/ref/alt", "requested region", "original request genome build"],
            "bam_patient_identity_independently_verified": False}


def _prepare_requests(db, data):
    requests, seen, seen_pairs = [], set(), set()
    warnings = []
    supplied = list(data.get("igv_requests") or [])
    legacy = data.get("schema") != "ngs_ai_review_return_v2"
    # Legacy files can express a request on the variant row only.
    pairs = {(r.get("record_key"), r.get("variant_key")) for r in supplied if isinstance(r, dict)}
    if legacy:
        for vr in data.get("variant_reviews") or []:
            if vr.get("igv_required") and (vr.get("record_key"), vr.get("variant_key")) not in pairs:
                supplied.append({**vr, "region": vr.get("igv_region") or "", "priority": vr.get("igv_priority") or "normal", "reason": vr.get("igv_reason") or vr.get("rationale") or ""})
    metadata_update = _obj(data.get("igv_request_set_update_policy") or data.get("request_set_update_policy")).get("kind") == "clinical_metadata_update_existing_request_set"
    for item in supplied:
        if not isinstance(item, dict):
            raise ValueError("IGV istek satırı nesne olmalıdır.")
        req = copy.deepcopy(item)
        identity = request_identity(req, data.get("genome_build") or "", allow_legacy=legacy)
        rid, rk, vk = identity["request_id"], identity["record_key"], identity["variant_key"]
        pair = (rk, vk)
        if rid in seen or pair in seen_pairs:
            raise ValueError("Yinelenen IGV istek kimliği veya teknik çalışma/varyant çifti.")
        seen.add(rid); seen_pairs.add(pair)
        sample = db.get_sample(rk)
        if not sample:
            warnings.append("IGV çalışma eşleşmedi; kuyruk oluşturulmadı: " + rk)
            continue
        if req.get("patient_id") and _text(req["patient_id"]).upper() != _text(sample.get("patient_id")).upper():
            raise ValueError("IGV örnek etiketi record_key ile uyuşmuyor: " + _text(req["patient_id"]))
        prior = _registered(db, rid)
        if prior:
            prior_identity = {**prior, "request_revision": int(prior["request_revision"])}
            if _identity_fields(prior_identity) != _identity_fields(identity):
                raise ValueError("IGV request_id farklı çalışma/varyant/revizyon/lokus için yeniden kullanılmış: " + rid)
        old = db.cx.execute("SELECT * FROM igv_queue WHERE record_key=? AND variant_key=?", pair).fetchone()
        old = dict(old) if old else {}
        conflicts = db.cx.execute("SELECT record_key,variant_key FROM igv_queue WHERE request_id=? AND (record_key!=? OR variant_key!=?)", (rid, rk, vk)).fetchall()
        if conflicts:
            raise ValueError("IGV request_id başka teknik çalışma/varyanta bağlı: " + rid)
        old_id = _text(old.get("request_id"))
        old_rev = int(old.get("request_revision") or 0)
        original_request = _original_queue_request(db, old) if old else {}
        if old_id == rid and original_request:
            if _identity_fields(request_identity(original_request, allow_legacy=True)) != _identity_fields(identity):
                raise ValueError("Yeni belge eski IGV isteğinin kimlik/koordinatlarını değiştiriyor: " + rid)
        replace = False
        if old_id and old_id != rid:
            if identity["request_revision"] <= old_rev:
                raise ValueError("Eski/çelişkili IGV isteği güncel isteği değiştiremez: " + rid)
            if identity["parent_request_id"] != old_id:
                raise ValueError("Yeni IGV görüntü isteği parent_request_id ile önceki isteğe bağlanmalıdır.")
            if metadata_update:
                raise ValueError("Klinik metadata güncellemesi mevcut IGV istek kimliğini değiştiremez.")
            replace = True
        elif old_id == rid and old_rev != identity["request_revision"]:
            raise ValueError("Aynı request_id için revizyon değiştirilemez; yeni görüntüye yeni request_id gerekir.")
        elif not old_id and identity["request_revision"] > 1 and not identity["parent_request_id"]:
            raise ValueError("IGV revizyonu 1'den büyükse parent_request_id zorunludur.")
        if old.get("genome_build") and canonical_build(old["genome_build"]) != identity["genome_build"] and not replace:
            raise ValueError("Mevcut IGV kanıtının genom sürümü istekle uyuşmuyor.")
        execution = dict(_obj(req.get("execution")))
        if execution.get("reset_manual_decision"):
            warnings.append("YZ isteği manuel patolog kararını sıfırlayamaz; karar geçmişi korundu.")
        # Reimporting an identical request is never a new capture, even if its original request asked for one.
        execution.update(reuse_existing_snapshot=not replace, force_new_task=replace, reset_manual_decision=False)
        req.update(identity); req["execution"] = execution
        if not replace and original_request:
            link = _obj(original_request.get("legacy_capture_linkage")) or _legacy_capture_linkage(
                _capture_metadata(old.get("capture_meta_path")), original_request, old,
                "reconstructed_from_local_capture_and_original_request")
            if link:
                req["legacy_capture_linkage"] = link
        requests.append((req, old, replace))
    return requests, warnings


def _store_requests(db, requests, session_id):
    timestamp = _now()
    for req, old, replace in requests:
        identity = request_identity(req, allow_legacy=True)
        rid, rk, vk = req["request_id"], req["record_key"], req["variant_key"]
        if replace:
            db.cx.execute("INSERT INTO igv_request_history_v0481(queue_id,request_id,request_revision,superseded_by_request_id,queue_snapshot_json,archived_at) VALUES(?,?,?,?,?,?)",
                          (old["id"], old.get("request_id"), old.get("request_revision"), rid, _json(old), timestamp))
        db.cx.execute("""INSERT INTO igv_request_registry_v0481(request_id,request_revision,record_key,variant_key,genome_build,region,parent_request_id,identity_origin,payload_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                      (rid, req["request_revision"], rk, vk, req["genome_build"], req["region"], req["parent_request_id"], req["request_identity_origin"], _json(req), timestamp, timestamp))
        db.cx.execute("""INSERT INTO igv_queue(record_key,variant_key,patient_link_id,patient_id,gene,change,region,priority,reason,source_session_id,status,created_at,updated_at,request_id,request_revision,parent_request_id,request_payload_json,genome_build,source_identity_json,pileup_json,coverage_audit_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(record_key,variant_key) DO UPDATE SET patient_link_id=excluded.patient_link_id,patient_id=excluded.patient_id,
          gene=excluded.gene,change=excluded.change,region=excluded.region,priority=excluded.priority,reason=excluded.reason,
          source_session_id=excluded.source_session_id,updated_at=excluded.updated_at,request_id=excluded.request_id,
          request_revision=excluded.request_revision,parent_request_id=excluded.parent_request_id,request_payload_json=excluded.request_payload_json,
          genome_build=excluded.genome_build,source_identity_json=excluded.source_identity_json,pileup_json=excluded.pileup_json,coverage_audit_json=excluded.coverage_audit_json""",
                      (rk, vk, req.get("patient_link_id") or "", req.get("patient_id") or "", req.get("gene") or "", req.get("change") or "", req["region"], req.get("priority") or "normal", req.get("reason") or "", session_id, timestamp, timestamp, rid, req["request_revision"], req["parent_request_id"], _json(req), req["genome_build"], _json(req.get("source_files") or {}), _json(req.get("pileup") or {}), _json(req.get("coverage_audit") or {})))
        if replace:
            db.cx.execute("""UPDATE igv_queue SET status='pending',screenshot_path='',capture_meta_path='',auto_settings_json='',ai_ready=0,capture_error='',captured_at='',manual_decision_request_id=? WHERE record_key=? AND variant_key=?""",
                          (old.get("manual_decision_request_id") or old.get("request_id") or "", rk, vk))


def import_ai_review(db, data, source_path="", source_sha256=""):
    """Transactional replacement for the older request-reset import layers."""
    ensure_igv_workflow_schema(db)
    if data.get("schema") in REVIEW_SCHEMAS:
        result = import_igv_reviews(db, data, data.get("_igv_evidence") or {}, source_sha256)
        result.update(session_id=result.get("session_id") or 0, patient_reviews=0, variant_reviews=0, cnv_reviews=0, artifact_reviews=0, igv_requests=0)
        return result
    requests, warnings = _prepare_requests(db, data)
    doc_hash = _hash(data)
    prior = db.cx.execute("SELECT session_id FROM ai_import_documents_v0481 WHERE content_sha256=?", (doc_hash,)).fetchone()
    if prior:
        return {"session_id": prior["session_id"], "patient_reviews": 0, "variant_reviews": 0, "cnv_reviews": 0,
                "artifact_reviews": 0, "igv_requests": len(requests), "duplicate_import": True,
                "igv_reviews_imported": 0, "warnings": ["Aynı YZ dönüşü daha önce kaydedilmiş; kayıt ve görüntüler çoğaltılmadı."]}
    src, pool = _obj(data.get("source_bundle")), _obj(data.get("pool_review"))
    timestamp = _now()
    counts = {"patient_reviews": 0, "variant_reviews": 0, "cnv_reviews": 0, "artifact_reviews": 0}
    try:
        db.cx.execute("BEGIN IMMEDIATE")
        cur = db.cx.execute("INSERT INTO ai_review_sessions(imported_at,schema_name,pool_group,sample_scope,source_path,source_sha256,pool_risk,overall_summary,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                            (timestamp, data.get("schema") or "", src.get("pool_group") or "", src.get("sample_scope") or "", source_path, source_sha256, pool.get("risk_level") or "", pool.get("summary") or "", _json(data)))
        sid = int(cur.lastrowid)
        for pr in data.get("patient_reviews") or []:
            db.cx.execute("INSERT INTO ai_patient_reviews(session_id,record_key,patient_link_id,patient_id,sample_type,release_recommendation,summary,notes_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (sid, pr.get("record_key") or "", pr.get("patient_link_id") or "", pr.get("patient_id") or "", pr.get("sample_type") or "", pr.get("release_recommendation") or "", pr.get("summary") or "", _json(pr.get("notes") or []), timestamp))
            counts["patient_reviews"] += 1
        for vr in data.get("variant_reviews") or []:
            if not vr.get("record_key") or not vr.get("variant_key"):
                raise ValueError("YZ varyantında çalışma/varyant kimliği eksik.")
            db.cx.execute("INSERT INTO ai_variant_reviews(session_id,record_key,patient_link_id,patient_id,variant_key,gene,change,review_status,confidence,report_recommendation,rationale,igv_required,igv_reason,igv_priority,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, vr["record_key"], vr.get("patient_link_id") or "", vr.get("patient_id") or "", vr["variant_key"], vr.get("gene") or "", vr.get("change") or "", vr.get("review_status") or "", vr.get("confidence") or "", vr.get("report_recommendation") or "", vr.get("rationale") or "", int(bool(vr.get("igv_required"))), vr.get("igv_reason") or "", vr.get("igv_priority") or "", _json(vr), timestamp))
            counts["variant_reviews"] += 1
        for cnv in data.get("cnv_reviews") or []:
            if isinstance(cnv, dict) and not cnv.get('cnv_key') and str(cnv.get('event_key') or '').startswith('CNV|'):
                cnv = {**cnv, 'cnv_key': cnv['event_key']}  # NGS_CNV_EVENT_KEY_ALIAS_V5
            if not cnv.get("record_key") or not cnv.get("cnv_key"):
                warnings.append("Kimliği eksik CNV incelemesi ham belgede korundu; CNV tablosuna yazılmadı.")
                continue
            db.cx.execute("INSERT INTO ai_cnv_reviews_v0481(session_id,record_key,cnv_key,raw_json,imported_at) VALUES(?,?,?,?,?)",
                          (sid, cnv["record_key"], cnv["cnv_key"], _json(cnv), timestamp)); counts["cnv_reviews"] += 1
        for ar in data.get("artifact_reviews") or []:
            if not ar.get("variant_key"):
                continue
            db.cx.execute("INSERT INTO ai_artifact_reviews(session_id,variant_key,gene,change,status,confidence,reason,affected_patient_ids_json,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (sid, ar["variant_key"], ar.get("gene") or "", ar.get("change") or "", ar.get("status") or "", ar.get("confidence") or "", ar.get("reason") or "", _json(ar.get("affected_patient_ids") or []), _json(ar), timestamp)); counts["artifact_reviews"] += 1
        _store_requests(db, requests, sid)
        review_result = {"igv_reviews_imported": 0, "igv_reviews_duplicate": 0, "igv_reviews_unresolved": 0}
        supplement = _obj(data.get("_igv_review_supplement"))
        if supplement.get("review"):
            review_result = import_igv_reviews(db, supplement["review"], supplement.get("evidence") or {}, source_sha256, ensure_schema=False, commit=False)
            warnings.extend(review_result.pop("warnings", []))
        db.cx.execute("INSERT INTO ai_import_documents_v0481(content_sha256,session_id,imported_at) VALUES(?,?,?)", (doc_hash, sid, timestamp))
        db.cx.commit()
        return {"session_id": sid, **counts, "igv_requests": len(requests), **review_result,
                "duplicate_import": False, "warnings": warnings}
    except Exception:
        db.cx.rollback()
        raise


def _legacy_link(db, review, source_bundle, evidence):
    """Only explicit full linkage or an exported bundle mapping can resolve a v1 id."""
    legacy_id = _text(review.get("id"))
    visual = _obj(evidence.get("igv_visual_review") or evidence)
    linkage = _obj(visual.get("v2_request_linkage"))
    provenance = _obj(visual.get("source_provenance"))
    expected = _obj(visual.get("expected_variant"))
    if linkage and provenance.get("legacy_capture_id") == legacy_id:
        if visual.get("source_zip") and Path(_text(visual["source_zip"])).stem != source_bundle:
            raise ValueError("Eski IGV değerlendirmesi ile kimlik kanıtının kaynak paketleri farklı.")
        if linkage.get("status") != "unique_exact_variant_match_reconstructed_from_v1_fields" or linkage.get("match_count_among_existing_31_requests") != 1:
            raise ValueError("Eski IGV dönüşünün tekil eşleşmesi doğrulanmamış.")
        build = canonical_build(expected.get("genome_build"))
        vk = _text(linkage.get("matched_variant_key"))
        expected_vk = "|".join(str(expected.get(k) or "") for k in ("chrom", "start", "ref", "alt"))
        if not build or expected_vk != vk or not linkage.get("matching_fields"):
            raise ValueError("Eski IGV dönüşünün genom/allel/lokus kanıtı tutarsız.")
        ident = {"request_id": linkage.get("matched_prior_request_id"), "request_revision": linkage.get("matched_prior_request_revision"),
                 "record_key": linkage.get("matched_record_key"), "variant_key": vk, "genome_build": build}
        observed = {"origin": "reconstructed_from_legacy_fields", "explicit_capture_revision_verified": False,
                    "unique_match_count": 1, "matching_fields": linkage["matching_fields"],
                    "legacy_id": legacy_id, "source_evidence_kind": "user_supplied_review_evidence",
                    "bam_patient_identity_independently_verified": False}
        return ident, observed, visual
    rows = db.cx.execute("SELECT identity_json FROM igv_bundle_identity_v0481 WHERE bundle_name=? AND legacy_id=?", (source_bundle, legacy_id)).fetchall()
    identities = [_decoded(r["identity_json"]) for r in rows]
    if len(identities) != 1:
        raise ValueError("Eski IGV id tek başına çalışma/allel/revizyonu doğrulamaz; tam kimlik kanıtı veya tekil paket eşlemesi gerekli.")
    return identities[0], {"origin": "reconstructed_from_exported_bundle_mapping", "unique_match_count": 1,
                           "explicit_capture_revision_verified": False, "legacy_id": legacy_id}, {}


def validate_review_document(data):
    errors = []
    if not isinstance(data, dict) or data.get("schema") not in REVIEW_SCHEMAS:
        return ["IGV değerlendirmesi ngs_igv_review_v1/v2 şemasında olmalıdır."]
    if not isinstance(data.get("reviews"), list):
        return ["IGV reviews liste olmalıdır."]
    for i, row in enumerate(data["reviews"]):
        if not isinstance(row, dict):
            errors.append(f"IGV reviews[{i}] nesne olmalıdır."); continue
        if row.get("evidence") not in ("supports_call", "does_not_support", "indeterminate"):
            errors.append(f"IGV reviews[{i}] evidence geçersiz.")
        if row.get("confidence") not in ("high", "medium", "low", "indeterminate"):
            errors.append(f"IGV reviews[{i}] confidence geçersiz.")
        if not isinstance(row.get("manual_review"), bool):
            errors.append(f"IGV reviews[{i}] manual_review true/false olmalıdır.")
        if data["schema"] == "ngs_igv_review_v2":
            for key in ("request_id", "request_revision", "record_key", "variant_key", "genome_build"):
                if not row.get(key):
                    errors.append(f"IGV reviews[{i}] {key} zorunludur.")
    return errors


def import_igv_reviews(db, document, evidence=None, source_sha256="", ensure_schema=True, commit=True):
    if ensure_schema:
        ensure_igv_workflow_schema(db)
    errors = validate_review_document(document)
    if errors:
        raise ValueError("\n".join(errors))
    result = {"igv_reviews_imported": 0, "igv_reviews_duplicate": 0, "igv_reviews_unresolved": 0, "warnings": []}
    source_bundle = _text(document.get("source_bundle"))
    for row in document["reviews"]:
        result_key = _hash({"source_bundle": source_bundle, "review": row})
        prior = db.cx.execute("SELECT id FROM igv_review_results_v0481 WHERE result_key=?", (result_key,)).fetchone()
        if prior:
            result["igv_reviews_duplicate"] += 1; continue
        try:
            if document["schema"] == "ngs_igv_review_v2":
                ident = {k: row.get(k) for k in ("request_id", "request_revision", "record_key", "variant_key", "genome_build")}
                linkage, visual = {"origin": "explicit_request_identity", "explicit_capture_revision_verified": True}, {}
            else:
                ident, linkage, visual = _legacy_link(db, row, source_bundle, _obj(evidence))
            ident["request_revision"] = _revision(ident.get("request_revision"))
            ident["genome_build"] = canonical_build(ident.get("genome_build"))
            registered = _registered(db, _text(ident.get("request_id")))
            if not registered:
                raise ValueError("IGV isteği yerel kayıtta bulunamadı.")
            for key in ("request_id", "request_revision", "record_key", "variant_key", "genome_build"):
                if ident.get(key) != registered.get(key):
                    raise ValueError("IGV dönüşü istek kimliğiyle uyuşmuyor: " + key)
            q = db.cx.execute("SELECT * FROM igv_queue WHERE record_key=? AND variant_key=?", (ident["record_key"], ident["variant_key"])).fetchone()
            q = dict(q) if q else {}
            if q.get("request_id") != ident["request_id"] or int(q.get("request_revision") or 0) != ident["request_revision"]:
                raise ValueError("Eski IGV revizyonunun dönüşü güncel isteğe uygulanamaz.")
            provenance = _obj(visual.get("source_provenance"))
            capture = _obj(row.get("source_capture"))
            capture_state = _text(capture.get("status") or row.get("capture_status") or provenance.get("capture_status")) or "unknown"
            if capture_state not in ("complete", "partial", "missing", "pending", "error", "unknown"):
                raise ValueError("IGV çekim durumu geçersiz.")
            capture_state_origin = "review_document"
            map_row = db.cx.execute("SELECT identity_json FROM igv_bundle_identity_v0481 WHERE bundle_name=? AND request_id=?", (source_bundle, ident["request_id"])).fetchall()
            mapped = _decoded(map_row[0]["identity_json"]) if len(map_row) == 1 else {}
            known_state = mapped.get("capture_status") or provenance.get("capture_status")
            if known_state in ("complete", "partial", "missing"):
                if capture_state != "unknown" and capture_state != known_state:
                    result["warnings"].append(ident["request_id"] + ": YZ çekim durumu kaynakla farklı; kaynak çekim durumu korundu.")
                capture_state = known_state
                capture_state_origin = "exported_bundle" if mapped.get("capture_status") else "user_supplied_capture_evidence"
            source_metrics = _obj(row.get("source_metrics") or visual.get("source_call_preserved"))
            measured = _obj(row.get("independently_measured_metrics") or visual.get("image_derived_quantitative_metrics"))
            if not measured:
                measured = {"status": "not_computed", "dp": None, "alt_ad": None, "vaf": None, "forward_alt": None, "reverse_alt": None}
            if measured.get("status") not in ("ok", "measured"):
                numeric = _obj(measured.get("values")) or {k: v for k, v in measured.items() if k != "status"}
                if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numeric.values()):
                    raise ValueError("Ölçülmemiş IGV alanlarında sayısal pileup sonucu kabul edilmez.")
            capture_detail = {**capture, "status": capture_state, "status_origin": capture_state_origin}
            if visual.get("zoom_review"):
                capture_detail["available_zooms"] = [z["zoom"] for z in visual["zoom_review"] if z.get("available")]
                capture_detail["missing_zooms"] = [z["zoom"] for z in visual["zoom_review"] if not z.get("available")]
            raw = {"review": row, "request_identity": ident, "source_capture": capture_detail,
                   "clinical_report_release": False, "source_metrics": source_metrics,
                   "independently_measured_metrics": measured}
            db.cx.execute("""INSERT INTO igv_review_results_v0481(result_key,request_id,request_revision,record_key,variant_key,genome_build,queue_id,evidence,confidence,manual_review,capture_status,clinical_report_release,linkage_json,source_metrics_json,measured_metrics_json,raw_json,source_bundle,source_sha256,imported_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                          (result_key, ident["request_id"], ident["request_revision"], ident["record_key"], ident["variant_key"], ident["genome_build"], q["id"], row["evidence"], row["confidence"], int(row["manual_review"]), capture_state, _json(linkage), _json(source_metrics), _json(measured), _json(raw), source_bundle, source_sha256, _now()))
            db.cx.execute("DELETE FROM igv_unmatched_reviews_v0481 WHERE result_key=?", (result_key,))
            if linkage.get("origin") == "reconstructed_from_legacy_fields":
                payload = _decoded(q.get("request_payload_json"))
                payload["legacy_capture_linkage"] = {**linkage, "request_identity": {**ident, "region": registered.get("region") or ""}}
                db.cx.execute("UPDATE igv_queue SET request_payload_json=? WHERE id=?", (_json(payload), q["id"]))
            result["igv_reviews_imported"] += 1
            result["session_id"] = q.get("source_session_id") or result.get("session_id")
        except (ValueError, TypeError) as exc:
            result["igv_reviews_unresolved"] += 1
            msg = _text(row.get("request_id") or row.get("id")) + ": " + str(exc)
            result["warnings"].append(msg)
            db.cx.execute("INSERT INTO igv_unmatched_reviews_v0481(result_key,reason,raw_json,imported_at) VALUES(?,?,?,?) ON CONFLICT(result_key) DO UPDATE SET reason=excluded.reason,raw_json=excluded.raw_json",
                          (result_key, msg, _json(row), _now()))
    if commit:
        db.cx.commit()
    return result


def current_igv_review(db, queue_row):
    q = dict(queue_row)
    row = db.cx.execute("SELECT * FROM igv_review_results_v0481 WHERE request_id=? AND request_revision=? AND record_key=? AND variant_key=? ORDER BY id DESC LIMIT 1",
                        (q.get("request_id") or "", int(q.get("request_revision") or 0), q.get("record_key") or "", q.get("variant_key") or "")).fetchone()
    if not row:
        return {}
    out = dict(row)
    for key in ("linkage_json", "source_metrics_json", "measured_metrics_json", "raw_json"):
        out[key.removesuffix("_json")] = _decoded(out.pop(key))
    out["manual_review"] = bool(out["manual_review"])
    out["clinical_report_release"] = False
    return out


def export_request(db, queue_row):
    """Materialize old request metadata only from its saved original AI document."""
    ensure_igv_workflow_schema(db)
    q = dict(queue_row)
    payload = _original_queue_request(db, q)
    if not payload or not payload.get("request_id"):
        raise ValueError("IGV isteğinin tam kimliği eksik; ilgili IGV_REQUEST_V2 / YZ dönüşünü yeniden içe alın.")
    candidates, warnings = _prepare_requests(db, {"schema": "ngs_ai_review_return_v2", "genome_build": payload.get("genome_build") or _obj(payload.get("coordinates")).get("genome_build"), "igv_requests": [payload]})
    if len(candidates) != 1:
        raise ValueError("IGV isteği mevcut teknik çalışmayla tekil eşleşmedi.")
    _store_requests(db, candidates, q.get("source_session_id") or 0)
    db.cx.commit()
    latest = db.get_igv_queue_item(q["id"])
    return _decoded(latest.get("request_payload_json")), latest


def prepare_capture_request(db, queue_row):
    """Upgrade an existing queue from its saved request before a first v0.4.81 capture."""
    if not queue_row or not queue_row.get("id"):
        return dict(queue_row or {})
    return export_request(db, queue_row)[1]


def igv_scope_summary(db, record_key=None, session_id=None):
    ensure_igv_workflow_schema(db)
    sql, args = "SELECT * FROM igv_queue WHERE status!='dismissed'", []
    if record_key:
        sql += " AND record_key=?"; args.append(record_key)
    if session_id is not None:
        sql += " AND source_session_id=?"; args.append(int(session_id))
    rows = [dict(r) for r in db.cx.execute(sql, args).fetchall()]
    out = {"requested_count": len(rows), "captured_count": 0, "local_capture_count": 0, "complete_count": 0,
           "partial_count": 0, "missing_count": 0, "reviewed_count": 0, "manual_review_required_count": 0,
           "clinical_report_release": False, "details": []}
    for q in rows:
        meta = _capture_metadata(q.get("capture_meta_path"))
        if not capture_matches_request(meta, q, q["record_key"], q["variant_key"]):
            meta = {}
        available = [z for z in ZOOMS if any(x.get("zoom") == z and x.get("status") == "complete" and Path(_text(x.get("path"))).is_file() for x in meta.get("images") or [] if isinstance(x, dict))]
        local_state = "complete" if len(available) == 4 else "partial" if available else "missing"
        review = current_igv_review(db, q)
        state = local_state if available else review.get("capture_status") or "missing"
        if state not in ("complete", "partial"):
            state = "missing"
        out["local_capture_count"] += int(bool(available))
        out["captured_count"] += int(state in ("complete", "partial"))
        out[state + "_count"] += 1
        out["reviewed_count"] += int(bool(review))
        out["manual_review_required_count"] += int(bool(review.get("manual_review")))
        out["details"].append({"request_id": q.get("request_id") or "", "request_revision": q.get("request_revision") or 0,
                               "record_key": q["record_key"], "variant_key": q["variant_key"], "genome_build": q.get("genome_build") or "",
                               "capture_status": state, "local_capture_status": local_state, "available_local_zooms": available,
                               "evidence": review.get("evidence"), "confidence": review.get("confidence"),
                               "manual_review": review.get("manual_review"), "clinical_report_release": False})
    out["scope_note"] = f"{out['reviewed_count']}/{out['requested_count']} istek değerlendirildi; {out['requested_count'] - out['reviewed_count']} istek için değerlendirme yok."
    return out


def _capture_metadata(path):
    raw = _text(path)
    if not raw:
        return {}
    try:
        doc = json.loads(Path(raw).read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) and doc.get("schema") in ("ngs_igv_capture_v1", "ngs_igv_capture_v2") else {}
    except (ValueError, OSError):
        return {}


def capture_matches_request(capture, queue_row, record_key, variant_key):
    if not capture or capture.get("record_key") != record_key or capture.get("variant_key") != variant_key:
        return False
    q = dict(queue_row)
    rid = _text(q.get("request_id"))
    if rid:
        if capture.get("request_id"):
            return (capture.get("request_id") == rid and int(capture.get("request_revision") or 0) == int(q.get("request_revision") or 0)
                    and canonical_build(capture.get("genome_build")) == canonical_build(q.get("genome_build")))
        payload = _decoded(q.get("request_payload_json"))
        link = _obj(payload.get("legacy_capture_linkage"))
        ident = _obj(link.get("request_identity"))
        if not link or link.get("unique_match_count") != 1 or link.get("legacy_id") != capture.get("id"):
            return False
        if any(ident.get(key) != q.get(key) for key in ("request_id", "request_revision", "record_key", "variant_key", "genome_build")):
            return False
        coords = _obj(payload.get("coordinates")); target = _obj(capture.get("target"))
        return (str(target.get("chrom") or "").lower().removeprefix("chr") == str(coords.get("chrom") or "").lower().removeprefix("chr")
                and all(str(target.get(a)) == str(coords.get(b)) for a, b in (("pos", "position"), ("ref", "ref"), ("alt", "alt")))
                and _text(capture.get("requested_region")) == _text(q.get("region")))
    return not capture.get("request_id") and int(capture.get("queue_id") or 0) == int(q.get("id") or 0)


def prepare_capture_storage(default_directory, queue_row, record_key, variant_key):
    """Reuse a verified current capture regardless of a changed AI review session."""
    q = dict(queue_row)
    old = _capture_metadata(q.get("capture_meta_path"))
    if capture_matches_request(old, q, record_key, variant_key):
        if not old.get("request_id") and q.get("request_id"):
            old["request_linkage"] = _obj(_decoded(q.get("request_payload_json")).get("legacy_capture_linkage"))
            old["request_identity_origin"] = old["request_linkage"].get("origin") or "reconstructed_from_legacy_fields"
            old["request_revision_explicit_in_original_capture"] = False
        return Path(q["capture_meta_path"]).parent, old
    out = Path(default_directory)
    if q.get("request_id"):
        out = out.parent / (out.name + "_R" + str(q.get("request_revision") or 1) + "_" + _hash(q["request_id"])[:10])
    candidate = _capture_metadata(out / "CAPTURE.json")
    return out, candidate if capture_matches_request(candidate, q, record_key, variant_key) else {}


def bind_capture_identity(capture, queue_row):
    """Call before every capture write; old generator metadata remains historical."""
    from igv_capture_v1 import APP_VERSION, CAPTURE_ENGINE_VERSION
    q = dict(queue_row)
    capture["app_version"] = APP_VERSION
    capture["last_capture_engine_version"] = CAPTURE_ENGINE_VERSION
    capture.setdefault("capture_engine_version", capture.get("generator_version") or CAPTURE_ENGINE_VERSION)
    capture["schema_version"] = 2
    capture["schema"] = "ngs_igv_capture_v2"
    if q.get("request_id"):
        capture.update({k: q.get(k) for k in ("request_id", "request_revision", "record_key", "variant_key", "genome_build", "parent_request_id")})
        capture.setdefault("request_identity_origin", "carried_from_verified_queue_request")
    return capture


def update_capture_queue(db, original_queue, **fields):
    """A delayed capture may update only the request with which it started."""
    original = dict(original_queue)
    if "request_id" not in {r["name"] for r in db.cx.execute("PRAGMA table_info(igv_queue)").fetchall()}:
        ensure_igv_workflow_schema(db)
    allowed = {"status", "screenshot_path", "capture_meta_path", "auto_settings_json", "ai_ready", "capture_error", "captured_at"}
    if set(fields) - allowed:
        raise ValueError("IGV çekim güncellemesinde izin verilmeyen alan.")
    assignments = [name + "=?" for name in fields]
    values = list(fields.values())
    assignments.append("updated_at=?"); values.append(_now())
    values.extend([original.get("id"), original.get("request_id") or "", int(original.get("request_revision") or 0), original.get("record_key") or "", original.get("variant_key") or ""])
    cur = db.cx.execute("UPDATE igv_queue SET " + ",".join(assignments) + " WHERE id=? AND COALESCE(request_id,'')=? AND COALESCE(request_revision,0)=? AND record_key=? AND variant_key=?", values)
    if cur.rowcount != 1:
        db.cx.rollback()
        raise RuntimeError("IGV isteği çekim sırasında değişti; eski çekim yeni isteğin durumunu değiştirmedi.")
    db.cx.commit()


def update_igv_queue_item(db, item_id, status=None, screenshot_path=None, manual_decision=None, manual_note=None,
                         capture_meta_path=None, auto_settings_json=None, ai_ready=None, capture_error=None, captured_at=None):
    ensure_igv_workflow_schema(db)
    pairs = (("status", status), ("screenshot_path", screenshot_path), ("manual_decision", manual_decision),
             ("manual_note", manual_note), ("capture_meta_path", capture_meta_path), ("auto_settings_json", auto_settings_json),
             ("ai_ready", ai_ready), ("capture_error", capture_error), ("captured_at", captured_at))
    fields, values = [], []
    for key, value in pairs:
        if value is not None:
            fields.append(key + "=?"); values.append(value)
    if not fields:
        return
    if manual_decision is not None:
        fields.append("manual_decision_request_id=COALESCE(request_id,'')" if _text(manual_decision) else "manual_decision_request_id=''")
    fields.append("updated_at=?"); values.extend((_now(), int(item_id)))
    with db.cx:
        db.cx.execute("UPDATE igv_queue SET " + ",".join(fields) + " WHERE id=?", values)


def install_igv_workflow(cls):
    """Install once after base StateDB definitions, before clinical-context wrappers."""
    if getattr(cls, "_igv_workflow_v0481_installed", False):
        return
    cls.import_ai_review = import_ai_review
    cls.update_igv_queue_item = update_igv_queue_item
    cls.ensure_igv_workflow_schema = ensure_igv_workflow_schema
    cls.import_igv_reviews = import_igv_reviews
    cls.igv_scope_summary = igv_scope_summary
    cls._igv_workflow_v0481_installed = True
