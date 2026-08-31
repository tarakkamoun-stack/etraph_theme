# Synchro "Devis Chiffrage" <- chiffrage.etraph.com (miroir lecture seule).
#
# Doctrine (avis Kimi 30/08, ETRAPH/erp/data/kimi-chiffrage-box-30-08.txt) :
#   - Sens UNIQUE chiffrage -> ERP. L'ERP n'est jamais une source d'ecriture.
#   - Remplacement COMPLET par devis, en une transaction (delete + reinsert),
#     jamais de merge champ par champ. Idempotent.
#   - RIEN n'est recalcule cote ERP : pu_ds/k/pu_vente/totaux sont copies
#     tels que la vue source v_boq_tree les fournit. Les taux de change
#     (devis_taux) sont figes avec le devis, jamais re-sources depuis
#     Currency Exchange.
#   - Un devis deja marque valide/envoye cote ERP (statut_source dans
#     PROTECTED_STATUSES) n'est JAMAIS ecrase.
#   - Jamais de hard delete : un devis absent de la source est marque
#     statut_source='absent_source', pas supprime (audit).
#   - Alerte apres N echecs consecutifs (compteur en settings) : une synchro
#     silencieusement morte est le pire etat possible.
#
# Endpoint source : GET /api/erp/v1/devis (liste) + /api/erp/v1/devis/:id
# (detail : entete + taux + lignes v_boq_tree), Bearer token.
# Config : Single "Devis Chiffrage Sync Settings" (endpoint + token en
# Password, jamais en dur dans le code).
#
# Execution manuelle :
#   bench --site erp.etraph.com execute etraph_theme.devis_chiffrage_sync.sync_devis
import frappe

LOCK_KEY = "devis_chiffrage_sync_lock"
LOCK_TTL_SECONDS = 900  # 15 min >> duree attendue d'un run
PROTECTED_STATUSES = {"soumis", "cloture"}  # jamais ecrases (doctrine 30/08)

READ_ONLY_MESSAGE = (
    "Devis Chiffrage est un miroir en lecture seule de "
    "chiffrage.etraph.com. Toute modification doit se faire cote source ; "
    "ce document serait ecrase au prochain cycle de synchronisation."
)


def guard_write(doc, method):
    # Branche via hooks.py -> doc_events["Devis Chiffrage"] (validate +
    # before_insert). Les child tables (Ligne/Taux) passent par le parent.
    if not frappe.flags.get("in_sync"):
        frappe.throw(READ_ONLY_MESSAGE, frappe.PermissionError)


def guard_delete(doc, method):
    if not frappe.flags.get("in_sync"):
        frappe.throw(READ_ONLY_MESSAGE, frappe.PermissionError)


def _log(motif):
    frappe.logger("devis_chiffrage_sync").info(motif)


def _f(value):
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _settings():
    return frappe.get_single("Devis Chiffrage Sync Settings")


def _record_run(status, message, failure=False):
    s = "Devis Chiffrage Sync Settings"
    frappe.db.set_single_value(s, "last_sync_at", frappe.utils.now())
    frappe.db.set_single_value(s, "last_sync_status", status)
    frappe.db.set_single_value(s, "last_sync_message", message)
    if failure:
        count = (frappe.db.get_single_value(s, "consecutive_failures") or 0) + 1
        frappe.db.set_single_value(s, "consecutive_failures", count)
        threshold = frappe.db.get_single_value(s, "alert_after_failures") or 3
        if count >= threshold:
            _alert(count, message)
    else:
        frappe.db.set_single_value(s, "consecutive_failures", 0)
    frappe.db.commit()


def _alert(count, message):
    # Une synchro morte en silence est le pire etat possible (doctrine 4.1).
    # Error Log systematique (visible Desk) + email best-effort (SMTP pas
    # configure sur le site a ce jour -> le try protege le run).
    title = "Devis Chiffrage sync: {0} echecs consecutifs".format(count)
    frappe.log_error(title=title, message=message)
    email = frappe.db.get_single_value("Devis Chiffrage Sync Settings", "alert_email")
    if email:
        try:
            frappe.sendmail(recipients=[email], subject=title, message=message)
        except Exception:
            _log("envoi email d'alerte impossible (SMTP non configure ?)")


def sync_devis():
    """Point d'entree scheduler_events (hourly) + execution manuelle."""
    if frappe.cache().get_value(LOCK_KEY):
        _log("sync deja en cours (lock actif), run ignore")
        return
    frappe.cache().set_value(LOCK_KEY, 1, expires_in_sec=LOCK_TTL_SECONDS)
    try:
        _run_sync()
    finally:
        frappe.cache().delete_value(LOCK_KEY)


def _run_sync():
    settings = _settings()
    base_url = (settings.endpoint_base_url or "").rstrip("/")
    token = settings.get_password("sync_token", raise_exception=False)
    if not base_url or not token:
        _log("sync non configuree (endpoint_base_url ou sync_token vide)")
        _record_run("Non configure", "endpoint_base_url ou sync_token manquant")
        return

    import requests
    headers = {"Authorization": "Bearer " + token}
    try:
        resp = requests.get(base_url + "/api/erp/v1/devis", headers=headers, timeout=30)
        resp.raise_for_status()
        listing = resp.json().get("devis")
    except Exception as e:
        _log("fetch liste devis ECHOUE: " + type(e).__name__)
        frappe.log_error(title="Devis Chiffrage sync: fetch liste failed",
                         message=frappe.get_traceback())
        _record_run("Echec", "fetch /api/erp/v1/devis a echoue (" + type(e).__name__ + ")",
                    failure=True)
        return

    # Garde-fou : payload vide/invalide = on ne touche a RIEN (ni upsert ni
    # marquage d'absence). La base source contient au moins 1 devis ; une
    # liste vide signale un probleme source, pas une realite metier.
    if not isinstance(listing, list) or len(listing) == 0:
        _log("liste vide ou invalide -> sync ABORTEE, miroir inchange")
        _record_run("Aborte", "liste devis vide ou invalide, aucune modification",
                    failure=True)
        return

    incoming_refs = set()
    created, replaced, skipped_protected, errors = 0, 0, 0, 0

    for entry in listing:
        ref_ext = str(entry.get("id") or "")
        if not ref_ext:
            continue
        incoming_refs.add(ref_ext)
        try:
            outcome = _sync_one(ref_ext, base_url, headers)
            if outcome == "created":
                created += 1
            elif outcome == "replaced":
                replaced += 1
            elif outcome == "protected":
                skipped_protected += 1
        except Exception:
            errors += 1
            frappe.db.rollback()
            _log("sync devis ref={0} ECHOUEE".format(ref_ext))
            frappe.log_error(title="Devis Chiffrage sync: devis {0} failed".format(ref_ext),
                            message=frappe.get_traceback())

    # Absents de la source : soft-flag, jamais de delete (doctrine 4.6).
    marked_absent = 0
    existing = frappe.get_all("Devis Chiffrage",
                              fields=["name", "reference_externe", "statut_source"])
    frappe.flags.in_sync = True
    try:
        for row in existing:
            if row.reference_externe in incoming_refs:
                continue
            if row.statut_source in PROTECTED_STATUSES or row.statut_source == "absent_source":
                continue
            frappe.db.set_value("Devis Chiffrage", row.name,
                                "statut_source", "absent_source")
            marked_absent += 1
            _log("ref={0} absent de la source -> statut_source=absent_source".format(
                row.reference_externe))
        frappe.db.commit()
    finally:
        frappe.flags.in_sync = False

    summary = ("recus={0} crees={1} remplaces={2} proteges_ignores={3} "
               "marques_absents={4} erreurs={5}").format(
        len(listing), created, replaced, skipped_protected, marked_absent, errors)
    _log(summary)
    _record_run("OK" if errors == 0 else "OK avec erreurs", summary,
                failure=(errors > 0 and created + replaced == 0))


def _sync_one(ref_ext, base_url, headers):
    """Remplacement complet d'UN devis, en une transaction. Retourne
    'created' | 'replaced' | 'protected' | 'unchanged'."""
    import requests
    resp = requests.get(base_url + "/api/erp/v1/devis/" + ref_ext,
                        headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    devis = data["devis"]
    taux_rows = data.get("taux") or []
    lignes = data.get("lignes") or []

    existing_name = frappe.db.get_value("Devis Chiffrage",
                                        {"reference_externe": ref_ext}, "name")
    projet_existant = None
    if existing_name:
        current = frappe.db.get_value(
            "Devis Chiffrage", existing_name,
            ["statut_source", "projet"], as_dict=True)
        # Doctrine 4.2 : un devis deja valide/envoye n'est JAMAIS ecrase.
        if current.statut_source in PROTECTED_STATUSES:
            return "protected"
        # Le lien Project est un enrichissement cote ERP (pas dans la
        # source) : on le preserve a travers le remplacement.
        projet_existant = current.projet

    id_to_code = {str(l["id"]): l["code"] for l in lignes}

    frappe.flags.in_sync = True
    try:
        if existing_name:
            frappe.delete_doc("Devis Chiffrage", existing_name,
                              force=True, ignore_permissions=True)

        doc = frappe.new_doc("Devis Chiffrage")
        doc.reference_externe = ref_ext
        doc.projet = projet_existant
        doc.label = devis.get("label") or ""
        doc.code = devis.get("code") or ""
        doc.statut_source = devis.get("status") or ""
        doc.devise_finale = devis.get("devise_finale") or ""
        doc.derniere_synchro = frappe.utils.now()

        for t in taux_rows:
            doc.append("taux", {
                "devise": t.get("devise"),
                "taux_vers_finale": _f(t.get("taux_vers_finale")),
            })

        for l in lignes:
            parent_id = str(l["parent_id"]) if l.get("parent_id") is not None else None
            doc.append("lignes", {
                "code": l.get("code") or "",
                "parent_code": id_to_code.get(parent_id, "") if parent_id else "",
                "niveau": l.get("lvl") or 0,
                "node_type": l.get("node_type") or "",
                "designation": l.get("designation") or "",
                "unite": l.get("unit") or "",
                "quantite": _f(l.get("qty")),
                "pu_entreprise": _f(l.get("pu_ds")),
                "pu_vente": _f(l.get("pu_vente")),
                "total_vente": _f(l.get("total_vente")),
                "reference_externe_id": str(l.get("id") or ""),
                "coeff_k": _f(l.get("k")),
                "total_entreprise": _f(l.get("total_ds")),
            })

        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.flags.in_sync = False

    return "replaced" if existing_name else "created"
