# Synchro "Candidature Site Web" <- portail carrieres (etraph.com/careers).
#
# V1 (avis Kimi 31/08, decisions D1-D6) :
#   - Sens unique source -> ERP, snapshot complet (pas d'incremental).
#   - Remplacement complet par document (upsert par source_id).
#   - Purge par absence : tout source_id absent du snapshot = document
#     supprime (doc + File + fichier physique), SAUF garde-fou anti-rafle
#     (payload vide/invalide, ou suppressions > 20% du stock -> sync
#     entiere ABORTEE, rien n'est supprime, alerte loggee).
#   - CV telecharge en File prive uniquement si cv_source_ref a change.
#   - Journal de sync = source_id + timestamp + motif UNIQUEMENT, jamais
#     de PII (nom, tel, email, etc.) dans les logs.
#   - Enums (status/department) inconnus acceptes tels quels (champ Data
#     libre, pas de Select strict) : stockes bruts + une alerte est logguee,
#     jamais de plantage de la synchro pour une valeur inconnue.
#
# CE FICHIER N'EST PAS ENCORE BRANCHE EN scheduler_events (cf hooks.py).
# Tant que les 2 endpoints PHP cote Plesk n'existent pas (voir
# ETRAPH/site-carrieres/data/spec-endpoint-erp-sync.md), il n'y a rien a
# synchroniser. Executable manuellement pour test :
#   bench --site erp.etraph.com execute etraph_theme.candidature_sync.sync_candidatures
import frappe
import json

LOCK_KEY = "candidature_site_web_sync_lock"
LOCK_TTL_SECONDS = 900  # 15 min, largement > la duree attendue d'un run
DELETE_ABORT_RATIO = 0.20  # D4 : garde-fou anti-rafle, non negociable

# Miroir des cles connues au 31/08/2026 (careers-data/config.php). Une cle
# absente de ces listes N'EST PAS rejetee (D6 point 4) : elle est stockee
# brute et seulement signalee dans le journal de sync (jamais de PII).
KNOWN_STATUSES = {
    "new", "to_review", "shortlist", "interview", "hired", "pool", "rejected",
}
KNOWN_DEPARTMENTS = {
    "engineering", "works", "technical", "hse_qaqc", "procurement",
    "equipment", "admin_hr", "finance", "tender", "it", "skilled",
    "internship", "other",
}

# Champs de la fiche IA cote source (cv_fields() dans careers/api/_lib.php),
# mirrores 1:1 vers les fields ERP du meme nom (sauf collision avec le
# formulaire declare, cf mapping ci-dessous).
AI_FIELD_MAP = {
    "first_name": "first_name", "last_name": "last_name",
    "name_native": "name_native", "birth_date": "birth_date", "age": "age",
    "nationality": "nationality", "city": "city", "country": "country",
    "email": None,  # deja pris depuis form, l'IA ne fait que confirmer
    "phone": None,
    "linkedin": "linkedin",
    "desired_position": "desired_position",
    "suggested_department": None,  # informatif seulement, pas mirrore V1
    "last_position": "last_position", "last_employer": "last_employer",
    "last_period": "last_period", "years_experience": "years_experience",
    "seniority": "seniority", "sectors": "sectors",
    "highest_degree": "highest_degree", "degree_field": "degree_field",
    "school": "school", "graduation_year": "graduation_year",
    "software": "software", "skills": "skills",
    "certifications": "certifications", "languages": "languages",
    "driving_license": "driving_license", "availability": "availability",
    "salary_expectation": "salary_expectation",
    "libya_experience": "libya_experience",
    "international_experience": "international_experience",
    "summary": "summary", "strengths": "strengths",
    "watchpoints": "watchpoints", "cv_language": "cv_language",
    "ai_confidence": "ai_confidence",
}
JSON_LIST_FIELDS = {
    "sectors", "software", "skills", "certifications", "languages",
    "international_experience", "strengths", "watchpoints",
    "previous_applications",
}


def _log(motif, source_id=None):
    # Journal sans PII : source_id (identifiant opaque) + motif + horodatage
    # (l'horodatage est ajoute automatiquement par le logger Frappe).
    msg = motif if source_id is None else "source_id={0} {1}".format(source_id, motif)
    frappe.logger("candidature_sync").info(msg)


def _to_json_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return ""


def _as_yes_no_unknown(value):
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _parse_dt(value):
    # Les timestamps source sont en ISO 8601 (date('c') cote PHP, ex.
    # "2026-08-11T10:00:00+01:00") : MySQL refuse ce format brut sur une
    # colonne DATETIME. Un timestamp illisible ne doit jamais faire
    # planter tout l'upsert (D6) : on log et on laisse le champ vide.
    if not value:
        return None
    try:
        dt = frappe.utils.get_datetime(value)
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)  # MySQL DATETIME n'a pas de fuseau
        return dt
    except Exception:
        return None


def _settings():
    return frappe.get_single("Candidature Sync Settings")


def _get_credentials():
    settings = _settings()
    base_url = (settings.endpoint_base_url or "").rstrip("/")
    token = settings.get_password("sync_token", raise_exception=False)
    return base_url, token


def _record_run(status, message):
    frappe.db.set_single_value("Candidature Sync Settings", "last_sync_at", frappe.utils.now())
    frappe.db.set_single_value("Candidature Sync Settings", "last_sync_status", status)
    frappe.db.set_single_value("Candidature Sync Settings", "last_sync_message", message)
    frappe.db.commit()


def sync_candidatures():
    """Point d'entree. Pas encore appele par scheduler_events (V1)."""
    if frappe.cache().get_value(LOCK_KEY):
        _log("sync deja en cours (lock actif), run ignore")
        return
    frappe.cache().set_value(LOCK_KEY, 1, expires_in_sec=LOCK_TTL_SECONDS)
    try:
        _run_sync()
    finally:
        frappe.cache().delete_value(LOCK_KEY)


def _run_sync():
    base_url, token = _get_credentials()
    if not base_url or not token:
        _log("sync non configuree (endpoint_base_url ou sync_token vide dans Candidature Sync Settings)")
        _record_run("Non configure", "endpoint_base_url ou sync_token manquant")
        return

    import requests
    try:
        resp = requests.get(
            base_url + "/candidates.php",
            headers={"Authorization": "Bearer " + token},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        _log("fetch candidates.php ECHOUE: " + type(e).__name__)
        frappe.log_error(title="Candidature sync: fetch failed", message=frappe.get_traceback())
        _record_run("Echec", "fetch candidates.php a echoue (" + type(e).__name__ + ")")
        return

    candidates = payload.get("candidates")
    count_declared = payload.get("count")
    generated_at = payload.get("generated_at")

    # --- Garde-fou D4 : payload vide/invalide = sync abortee, rien touche ---
    if not isinstance(candidates, list) or len(candidates) == 0:
        _log("payload vide ou invalide -> sync ABORTEE (garde-fou D4), stock ERP inchange")
        _record_run("Aborte", "payload vide ou invalide, aucune modification appliquee")
        return
    if count_declared is not None and count_declared != len(candidates):
        _log("count declare ({0}) != taille reelle du tableau ({1}) -> sync ABORTEE".format(
            count_declared, len(candidates)))
        _record_run("Aborte", "incoherence count/checksum du payload, aucune modification appliquee")
        return

    existing_ids = set(frappe.get_all("Candidature Site Web", pluck="source_id"))
    incoming_ids = set()
    created, updated, upsert_errors, cv_errors = 0, 0, 0, 0

    frappe.flags.in_sync = True
    try:
        for c in candidates:
            sid = c.get("id") or c.get("source_id")
            if not sid:
                continue
            incoming_ids.add(sid)
            try:
                is_new = sid not in existing_ids
                cv_ok = _upsert_one(sid, c, base_url, token)
                created += 1 if is_new else 0
                updated += 0 if is_new else 1
                cv_errors += 0 if cv_ok else 1
            except Exception:
                upsert_errors += 1
                _log("upsert ECHOUE", sid)
                frappe.log_error(title="Candidature sync: upsert failed", message=frappe.get_traceback())

        # --- Purge par absence + garde-fou anti-rafle (D4) ---
        to_delete = existing_ids - incoming_ids
        if existing_ids and (len(to_delete) / len(existing_ids)) > DELETE_ABORT_RATIO:
            _log("suppressions ({0}/{1}) > seuil {2:.0%} -> PURGE ABORTEE, rien supprime".format(
                len(to_delete), len(existing_ids), DELETE_ABORT_RATIO))
            frappe.log_error(
                title="Candidature sync: purge abortee (anti-rafle)",
                message="{0} suppressions sur {1} documents existants depasse le seuil {2:.0%}".format(
                    len(to_delete), len(existing_ids), DELETE_ABORT_RATIO),
            )
            deleted = 0
        else:
            deleted = 0
            for sid in to_delete:
                try:
                    _purge_one(sid)
                    deleted += 1
                    _log("supprime (absent du snapshot)", sid)
                except Exception:
                    _log("suppression ECHOUEE", sid)
                    frappe.log_error(title="Candidature sync: delete failed", message=frappe.get_traceback())
        frappe.db.commit()
    finally:
        frappe.flags.in_sync = False

    summary = (
        "generated_at={0} recus={1} crees={2} maj={3} erreurs_upsert={4} "
        "erreurs_cv={5} supprimes={6} skip_purge_anti_rafle={7}"
    ).format(
        generated_at, len(candidates), created, updated, upsert_errors,
        cv_errors, deleted, len(to_delete) - deleted if to_delete else 0,
    )
    _log(summary)
    _record_run("OK" if upsert_errors == 0 else "OK avec erreurs", summary)


def _upsert_one(source_id, c, base_url, token):
    form = c.get("form") or {}
    ai = c.get("ai") or {}
    consent = c.get("consent") or {}
    cv = c.get("cv") or {}

    status = c.get("status") or ""
    department = form.get("department") or ""
    if status and status not in KNOWN_STATUSES:
        _log("statut inconnu de la source ('{0}') accepte tel quel".format(status), source_id)
    if department and department not in KNOWN_DEPARTMENTS:
        _log("departement inconnu de la source ('{0}') accepte tel quel".format(department), source_id)

    exists = frappe.db.exists("Candidature Site Web", source_id)
    doc = frappe.get_doc("Candidature Site Web", source_id) if exists else frappe.new_doc("Candidature Site Web")
    doc.source_id = source_id
    doc.created_at_source = _parse_dt(c.get("created_at"))
    doc.lang = c.get("lang") or ""
    doc.status = status
    doc.department = department
    doc.rh_note = c.get("rh_note") or ""

    doc.first_name = form.get("first_name") or ""
    doc.last_name = form.get("last_name") or ""
    doc.phone = form.get("phone") or ""
    doc.email = form.get("email") or ""
    doc.position = form.get("position") or ""
    doc.trade = form.get("trade") or ""
    doc.years_exp_declared = form.get("years_exp") or ""
    doc.last_employer_declared = form.get("last_employer") or ""
    doc.libya_available = form.get("libya_available") or ""
    doc.passport = form.get("passport") or ""

    doc.consent_given_at = _parse_dt(consent.get("given_at"))
    doc.consent_version = consent.get("version") or ""
    doc.consent_ip = consent.get("ip") or ""

    doc.ai_status = c.get("ai_status") or ""
    doc.ai_edited = 1 if c.get("ai_edited") else 0
    for src_field, doc_field in AI_FIELD_MAP.items():
        if not doc_field:
            continue
        value = ai.get(src_field)
        if doc_field == "libya_experience":
            value = _as_yes_no_unknown(value)
        elif doc_field in JSON_LIST_FIELDS:
            value = _to_json_text(value)
        # first_name/last_name ont deja une valeur formulaire (lignes plus
        # haut) : une fiche IA vide (photo de CV, echec DeepSeek) ne doit
        # pas les ecraser par du vide.
        if doc_field in ("first_name", "last_name") and not value:
            continue
        setattr(doc, doc_field, value if value is not None else "")

    doc.cv_original_name = cv.get("original_name") or ""
    doc.cv_ext = cv.get("ext") or ""
    doc.cv_size_human = cv.get("size_human") or ""
    doc.previous_applications = _to_json_text(c.get("previous_applications") or [])

    frappe.flags.in_sync = True
    if exists:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)

    return _maybe_sync_cv(doc, cv, base_url, token, source_id)


def _maybe_sync_cv(doc, cv, base_url, token, source_id):
    """Retourne False seulement si un CV etait attendu et son
    telechargement a echoue (compte pour cv_errors). True sinon
    (pas de CV a synchroniser, ou synchro OK/deja a jour)."""
    stored_name = cv.get("stored_name") or ""
    if not stored_name:
        return True  # pas de CV cote source pour cette candidature
    content_key = cv.get("content_key") or ""
    ext = cv.get("ext") or ""
    size = cv.get("size") or ""
    new_ref = "{0}|{1}|{2}".format(stored_name, size, content_key)

    if doc.cv_source_ref == new_ref and doc.cv_file:
        return True  # deja a jour, pas de retelechargement (D3)

    import requests
    try:
        resp = requests.get(
            base_url + "/cv.php",
            params={"id": source_id},
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.content
    except Exception:
        _log("telechargement CV ECHOUE, retry au prochain cycle", source_id)
        frappe.db.set_value(
            "Candidature Site Web", doc.name, "cv_sync_error",
            "Echec telechargement au dernier cycle, retry automatique au prochain passage.",
        )
        return False

    filename = "cv_{0}.{1}".format(source_id, ext or "bin")
    # Retire l'ancien fichier prive avant d'attacher le nouveau (D4 : le CV
    # suit le meme cycle de vie que le document).
    if doc.cv_file:
        try:
            old_files = frappe.get_all(
                "File",
                filters={"attached_to_doctype": "Candidature Site Web", "attached_to_name": doc.name},
                pluck="name",
            )
            for f in old_files:
                frappe.delete_doc("File", f, ignore_permissions=True, force=1)
        except Exception:
            pass

    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": filename,
        "attached_to_doctype": "Candidature Site Web",
        "attached_to_name": doc.name,
        "attached_to_field": "cv_file",
        "is_private": 1,
        "content": content,
    })
    file_doc.insert(ignore_permissions=True)
    frappe.db.set_value("Candidature Site Web", doc.name, "cv_file", file_doc.file_url)
    frappe.db.set_value("Candidature Site Web", doc.name, "cv_source_ref", new_ref)
    frappe.db.set_value("Candidature Site Web", doc.name, "cv_sync_error", "")
    return True


def _purge_one(source_id):
    # D4 : suppression doc + File + fichier physique. frappe.delete_doc
    # supprime aussi les File enfants attaches (attached_to_doctype/name)
    # et leur fichier physique sur disque.
    frappe.flags.in_sync = True
    frappe.delete_doc("Candidature Site Web", source_id, ignore_permissions=True, force=1)
