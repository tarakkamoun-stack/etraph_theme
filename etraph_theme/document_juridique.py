# Documentation juridique (01/09) : DocType "Document Juridique" + alerte email
# avant expiration + tuile portail. Contre-audit Kimi applique avant build :
#   - alert_level (transition) plutot qu'un compteur "jours depuis dernier envoi"
#     -> respecte la doctrine du workspace "silence si OK, alerte sur
#     transition, digest conditionnel" au lieu d'un rappel hebdo par document.
#   - digest hebdomadaire (lundi) = filet de securite anti-perte SMTP, PAS un
#     rappel par document.
#   - fichier joint jamais dans l'email (lien /app/... uniquement, exige login).
#   - hook sur File EN PLUS du hook sur le doc parent (fenetre upload->save,
#     remplacement de fichier).
#   - "actif" (au lieu de delete) pour retirer un document supplante/doublon
#     sans perdre la trace ni generer un digest hebdomadaire eternel.
#   - "notifier" (01/09, demande Tarak) : un document avec une date_fin
#     CONVENTIONNELLE (ex. extrait RNE repute valable 3 mois, pas une date
#     imprimee sur le document) doit rester consultable/trie mais ne doit PAS
#     declencher d'email -> filtre sur notifier=1 en plus de actif=1.
import frappe
from frappe.utils import getdate, today

DOCTYPE = "Document Juridique"
SETTINGS = "Document Juridique Settings"


def guard_document(doc, method=None):
    """Reprivatise le File attache si besoin (garde-fou en plus du hook File
    ci-dessous : couvre le cas ou le champ fichier a change dans ce meme
    save avant que le hook File n'ait tourne)."""
    if not doc.fichier:
        return
    for fname in frappe.get_all("File", filters={"file_url": doc.fichier}, pluck="name"):
        fdoc = frappe.get_doc("File", fname)
        if not fdoc.is_private:
            fdoc.is_private = 1
            fdoc.save(ignore_permissions=True)


def guard_file_private(doc, method=None):
    """Tout File attache a un Document Juridique doit etre prive, meme si
    l'utilisateur a decoche la case au moment de l'upload."""
    if doc.attached_to_doctype == DOCTYPE and not doc.is_private:
        doc.is_private = 1


def compute_alert_level(date_fin):
    if not date_fin:
        return ""
    days_left = (getdate(date_fin) - getdate(today())).days
    if days_left < 0:
        return "expire"
    if days_left <= 30:
        return "j30"
    return ""


def _log(msg):
    frappe.logger("document_juridique").info(msg)


def _send(recipients, subject, message, dry_run):
    if dry_run:
        _log("DRY-RUN, aurait envoye a {0} | {1}".format(recipients, subject))
        return
    frappe.sendmail(recipients=recipients, subject=subject, message=message, now=True)
    _log("envoye a {0} | {1}".format(recipients, subject))


def _doc_line(d):
    days_left = (getdate(d.date_fin) - getdate(today())).days
    delai = "expire depuis {0} j".format(-days_left) if days_left < 0 else "expire dans {0} j".format(days_left)
    url = frappe.utils.get_url_to_form(DOCTYPE, d.name)
    return "- {0} ({1}, {2}) — {3} — echeance {4} — {5}".format(
        d.nom_fr, d.entite or "-", d.type_piece or "-", delai, d.date_fin, url,
    )


def _transition_subject(transitions):
    return "[ETRAPH ERP] Documentation juridique : {0} changement(s) de statut".format(len(transitions))


def _transition_body(transitions):
    lines = ["Documents dont le statut d'expiration a change aujourd'hui :", ""]
    lines += [_doc_line(t) for t in transitions]
    return "\n".join(lines)


def _digest_subject(current):
    return "[ETRAPH ERP] Documentation juridique : rappel hebdo ({0} document(s) a surveiller)".format(len(current))


def _digest_body(current):
    lines = ["Rappel hebdomadaire (filet de securite) — documents actuellement a J-30 ou expires :", ""]
    lines += [_doc_line(d) for d in current]
    return "\n".join(lines)


def check_expirations():
    """Cron quotidien (hooks.py, scheduler_events['cron']). Recalcule
    alert_level pour chaque document actif, envoie un email de TRANSITION
    (uniquement les documents dont le niveau a change aujourd'hui) puis, le
    lundi, un digest de tous les documents actuellement en j30/expire (filet
    anti-perte SMTP, pas un rappel par document)."""
    settings = frappe.get_single(SETTINGS)
    dry_run = bool(settings.dry_run)
    recipients = [r.strip() for r in (settings.destinataires or "").split(",") if r.strip()]
    if not recipients:
        _log("aucun destinataire configure dans Document Juridique Settings, cycle ignore")
        return

    docs = frappe.get_all(
        DOCTYPE, filters={"actif": 1, "notifier": 1},
        fields=["name", "nom_fr", "date_fin", "alert_level", "entite", "type_piece"],
    )

    transitions = []
    for d in docs:
        new_level = compute_alert_level(d.date_fin)
        if new_level != (d.alert_level or ""):
            transitions.append(d)
            d.alert_level = new_level  # reflete dans la liste en memoire pour le digest ci-dessous
            if not dry_run:
                frappe.db.set_value(DOCTYPE, d.name, "alert_level", new_level)

    if transitions:
        _send(recipients, _transition_subject(transitions), _transition_body(transitions), dry_run)
    else:
        _log("aucune transition aujourd'hui")

    if frappe.utils.now_datetime().weekday() == 0:  # lundi
        current = [d for d in docs if compute_alert_level(d.date_fin) in ("j30", "expire")]
        if current:
            _send(recipients, _digest_subject(current), _digest_body(current), dry_run)
        else:
            _log("digest lundi : rien a signaler")

    if not dry_run:
        frappe.db.commit()
