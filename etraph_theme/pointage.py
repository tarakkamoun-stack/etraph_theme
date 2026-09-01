# -*- coding: utf-8 -*-
"""Pointage quotidien main d'oeuvre chantier — contrôleurs + API.

Mission Tarak 01/09, contre-audit Kimi GO-avec-corrections appliqué avant
build (prompt+réponse : ETRAPH/erp/data/kimi-*-pointage-01-09.txt) :
- Fiches `Ouvrier` par métier (Link `Metier Ouvrier`, extensible par Aymen),
  taux figé sur chaque ligne à l'insertion (doctrine taux de change caisse :
  changer la fiche ne retouche JAMAIS l'historique). Snapshot COMPLET
  (métier, type_taux, taux, devise, heures_reference) — Kimi D6.
- `Pointage Journalier` = UN doc par jour+chantier, unicité HORS feuilles
  annulées (Kimi D2.1 : une journée annulée doit pouvoir être refaite).
- Montant : Horaire = heures × taux ; Journalier = taux × heures /
  heures_reference (prorata pur, Kimi D3 — pas de palier, pas de champ jours).
- Heures bornées 0-16 (Kimi D2.3, tue la faute de frappe mobile), date pas
  dans le futur, devise homogène par feuille (Kimi D1.4).
- PAS de contrainte serveur projet→ouvrier sur les lignes (Kimi D2.2 : un
  renfort d'un autre chantier se pointe ; le pré-remplissage et le picker
  couvrent le cas courant).
- Statuts calqués caisse V3 : naît En attente, Aymen valide (unitaire/lot)
  via save = Version tracée, Validé = figé, jamais de delete (annulation
  motivée). Lignes à 0h = absents (exclus des totaux, comptés nulle part).
- AUCUN Mouvement Caisse automatique (double comptage) : rapprochement par
  rapport, la caisse type ses dépenses MO (champ categorie, Kimi D5).

⚠️ Piège Frappe qui structure ce fichier : `validate_higher_perm_levels`
s'exécute APRÈS les hooks validate et RÉINITIALISE les champs permlevel 1
que l'utilisateur ne peut pas écrire (à None sur un doc neuf). Les champs
figés (taux, montant, totaux) sont donc posés en `on_update` via db_set
(écriture serveur directe, hors du cycle permlevel), jamais dans validate.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, nowdate

VALIDATOR_ROLES = {"System Manager", "ETRAPH Chef de Projet"}
MAX_HEURES = 16.0  # Kimi D2.3 : borne anti-faute-de-frappe


def _is_validator(user=None):
    return bool(set(frappe.get_roles(user or frappe.session.user)) & VALIDATOR_ROLES)


def _permitted_projects(user=None):
    user = user or frappe.session.user
    perms = frappe.permissions.get_user_permissions(user=user)
    return {p.get("doc") for p in (perms.get("Project") or [])}


# ---------------------------------------------------------------- Ouvrier

def validate_ouvrier(doc, method=None):
    if doc.type_taux and doc.type_taux not in ("Horaire", "Journalier"):
        frappe.throw(_("Type de taux inconnu : {0}").format(doc.type_taux))
    if flt(doc.taux) < 0:
        frappe.throw(_("Le taux ne peut pas être négatif."))
    if not doc.devise:
        doc.devise = "LYD"
    if flt(doc.heures_reference) <= 0:
        doc.heures_reference = 8
    # Kimi D1.2 : anti-doublon SOUPLE (homonymes + turn-over BTP : le bon
    # réflexe est de RÉACTIVER une fiche, pas d'en recréer une).
    dup = frappe.get_all("Ouvrier",
                         filters={"nom_complet": doc.nom_complet,
                                  "name": ["!=", doc.name or ""]},
                         pluck="name", limit=1)
    if dup:
        frappe.msgprint(_("Un ouvrier nommé « {0} » existe déjà ({1}). S'il s'agit "
                          "de la même personne, réactiver sa fiche plutôt qu'en "
                          "créer une nouvelle.").format(doc.nom_complet, dup[0]),
                        indicator="orange")


# ------------------------------------------------------- Pointage Journalier

def _montant_ligne(type_taux, taux, heures, heures_reference):
    h = flt(heures)
    if h <= 0:
        return 0.0
    if type_taux == "Journalier":
        return flt(flt(taux) * h / (flt(heures_reference) or 8.0), 3)
    return flt(h * flt(taux), 3)


def validate_pointage(doc, method=None):
    """Contrôles + règles de statut. Les champs figés/montants sont posés
    en on_update (cf. entête du module)."""
    is_val = _is_validator()

    if getdate(doc.date) > getdate(nowdate()):
        frappe.throw(_("La date de pointage ne peut pas être dans le futur."))

    # Kimi D2.1 : unicité jour+chantier HORS feuilles annulées.
    dup = frappe.get_all(
        "Pointage Journalier",
        filters={"projet": doc.projet, "date": doc.date,
                 "name": ["!=", doc.name or ""],
                 "statut_validation": ["!=", "Annulé"]},
        pluck="name", limit=1)
    if dup:
        frappe.throw(_("Une feuille de pointage existe déjà pour {0} le {1} : {2}.")
                     .format(doc.projet, doc.date, dup[0]))

    # Périmètre du pointeur (ceinture en plus du scoping natif User Permission).
    if not is_val and doc.projet not in _permitted_projects():
        frappe.throw(_("Vous n'êtes pas autorisé à pointer sur le chantier {0}.")
                     .format(doc.projet), frappe.PermissionError)

    old = doc.get_doc_before_save() if not doc.is_new() else None
    old_devises = {l.name: l.devise for l in (old.lignes if old else [])}

    vus, devises = set(), set()
    for ligne in doc.lignes:
        if ligne.ouvrier in vus:
            frappe.throw(_("Ouvrier en double dans la feuille : {0}.").format(ligne.ouvrier))
        vus.add(ligne.ouvrier)
        if flt(ligne.heures) < 0 or flt(ligne.heures) > MAX_HEURES:
            frappe.throw(_("Heures invalides pour {0} : {1} (0 à {2}).")
                         .format(ligne.ouvrier, ligne.heures, int(MAX_HEURES)))
        # devise de la ligne : figée si la ligne existe, sinon celle de la fiche
        d = old_devises.get(ligne.name) or \
            frappe.db.get_value("Ouvrier", ligne.ouvrier, "devise") or "LYD"
        devises.add(d)

    # Kimi D1.4 : une feuille = UNE devise (jamais de somme multi-devises).
    if len(devises) > 1:
        frappe.throw(_("Devises mélangées dans la feuille ({0}) : une feuille de "
                       "pointage ne porte qu'une seule devise.")
                     .format(", ".join(sorted(devises))))

    doc.total_heures = flt(sum(flt(l.heures) for l in doc.lignes), 2)
    doc.nb_presents = sum(1 for l in doc.lignes if flt(l.heures) > 0)

    _apply_statut_rules(doc, is_val)


def _apply_statut_rules(doc, is_val):
    """Même grammaire que la caisse V3 : transitions bornées, Version tracée."""
    if doc.is_new():
        doc.statut_validation = "En attente"
        doc.pointeur = frappe.session.user
        doc.valide_par = None
        doc.date_validation = None
        return
    old = doc.get_doc_before_save()
    if not old:
        return
    old_st, new_st = old.statut_validation, doc.statut_validation
    if old_st != new_st:
        allowed = {("En attente", "Validé"), ("En attente", "Annulé"),
                   ("Validé", "Annulé")}
        if (old_st, new_st) not in allowed:
            frappe.throw(_("Transition de statut {0} → {1} interdite.").format(old_st, new_st))
        if not is_val:
            frappe.throw(_("Seul le chef de projet valide ou annule un pointage."))
        if new_st == "Annulé" and not (doc.remarque or "").strip():
            frappe.throw(_("Une annulation exige une remarque (motif)."))
        if new_st == "Validé":
            doc.valide_par = frappe.session.user
            doc.date_validation = now_datetime()
    elif old_st in ("Validé", "Annulé") and not _same_payload(doc, old):
        # figé pour TOUT le monde : une feuille validée ne se retouche pas,
        # elle s'annule (motif) puis on en recrée une.
        frappe.throw(_("Cette feuille est {0} : elle ne se modifie plus "
                       "(l'annuler avec motif puis en recréer une).")
                     .format(old_st.lower()))


def _same_payload(doc, old):
    if (doc.projet, str(doc.date), len(doc.lignes)) != \
            (old.projet, str(old.date), len(old.lignes)):
        return False
    for a, b in zip(doc.lignes, old.lignes):
        if (a.ouvrier, flt(a.heures)) != (b.ouvrier, flt(b.heures)):
            return False
    return True


def before_insert_pointage(doc, method=None):
    """Pré-remplissage terrain (Kimi Q4 : c'est la feature d'ADOPTION) :
    à la création, une ligne par ouvrier actif du chantier, heures 0."""
    if doc.lignes:
        return
    for nom in frappe.get_all("Ouvrier", filters={"projet": doc.projet, "actif": 1},
                              order_by="metier asc, nom_complet asc", pluck="name"):
        doc.append("lignes", {"ouvrier": nom, "heures": 0})


def on_update_pointage(doc, method=None):
    """Fige le snapshot complet à l'insertion des lignes + calcule
    montants/totaux. db_set direct (serveur) : hors du cycle permlevel,
    jamais influencé par ce que le client a envoyé (Kimi D6 : le recalcul
    utilise le taux DE LA LIGNE, jamais un re-fetch de la fiche)."""
    old = doc.get_doc_before_save()
    old_lignes = {l.name: l for l in (old.lignes if old else [])}

    total_m = 0.0
    devise = None
    for ligne in doc.lignes:
        prev = old_lignes.get(ligne.name)
        if prev is not None and prev.ouvrier == ligne.ouvrier:
            # ligne existante : le snapshot ne bouge JAMAIS
            fige = {"nom_ouvrier": prev.nom_ouvrier, "metier": prev.metier,
                    "type_taux": prev.type_taux, "taux": flt(prev.taux),
                    "devise": prev.devise,
                    "heures_reference": flt(prev.heures_reference) or 8}
        else:
            o = frappe.db.get_value(
                "Ouvrier", ligne.ouvrier,
                ["nom_complet", "metier", "type_taux", "taux", "devise",
                 "heures_reference"], as_dict=True)
            fige = {"nom_ouvrier": o.nom_complet, "metier": o.metier,
                    "type_taux": o.type_taux, "taux": flt(o.taux),
                    "devise": o.devise or "LYD",
                    "heures_reference": flt(o.heures_reference) or 8}
        fige["montant"] = _montant_ligne(fige["type_taux"], fige["taux"],
                                         ligne.heures, fige["heures_reference"])
        changed = {k: v for k, v in fige.items() if ligne.get(k) != v}
        if changed:
            ligne.db_set(changed, update_modified=False)
        total_m += flt(fige["montant"])
        devise = devise or fige["devise"]

    maj = {}
    if flt(doc.total_montant) != flt(total_m, 3):
        maj["total_montant"] = flt(total_m, 3)
    if (devise or "LYD") != (doc.devise or None):
        maj["devise"] = devise or "LYD"
    if maj:
        doc.db_set(maj, update_modified=False)


def on_trash_pointage(doc, method=None):
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Une feuille de pointage ne se supprime pas : elle s'ANNULE "
                       "(statut Annulé, motif en remarque)."))


# ---------------------------------------------------------------- API

def _assert_validator():
    if not _is_validator():
        frappe.throw(_("Action réservée au chef de projet."), frappe.PermissionError)


@frappe.whitelist()
def preremplir(name):
    """Ajoute les ouvriers actifs du chantier absents de la feuille (heures 0).
    Le contrôle `actif` se fait à l'AJOUT de ligne, jamais à la validation
    (Kimi Q2.7 : une feuille existante reste validable)."""
    doc = frappe.get_doc("Pointage Journalier", name)
    doc.check_permission("write")
    if doc.statut_validation != "En attente":
        frappe.throw(_("Feuille {0} : pré-remplissage impossible.").format(doc.statut_validation))
    deja = {l.ouvrier for l in doc.lignes}
    ajouts = 0
    for nom in frappe.get_all("Ouvrier", filters={"projet": doc.projet, "actif": 1},
                              order_by="metier asc, nom_complet asc", pluck="name"):
        if nom not in deja:
            doc.append("lignes", {"ouvrier": nom, "heures": 0})
            ajouts += 1
    if ajouts:
        doc.save()
    return {"ajouts": ajouts}


@frappe.whitelist()
def valider_pointage(name):
    _assert_validator()
    doc = frappe.get_doc("Pointage Journalier", name)
    if doc.statut_validation != "En attente":
        frappe.throw(_("{0} n'est pas en attente (statut : {1}).")
                     .format(name, doc.statut_validation))
    doc.statut_validation = "Validé"
    doc.save()
    return {"name": doc.name, "total_montant": doc.total_montant,
            "total_heures": doc.total_heures}


@frappe.whitelist()
def valider_lot(projet=None):
    """Valide toutes les feuilles En attente (option : un seul chantier)."""
    _assert_validator()
    filters = {"statut_validation": "En attente"}
    if projet:
        filters["projet"] = projet
    noms = frappe.get_all("Pointage Journalier", filters=filters, pluck="name")
    total = 0.0
    for n in noms:
        d = frappe.get_doc("Pointage Journalier", n)
        d.statut_validation = "Validé"
        d.save()
        total += flt(d.total_montant)
    return {"valides": len(noms), "total": flt(total, 3)}


@frappe.whitelist()
def annuler_pointage(name, motif):
    _assert_validator()
    if not (motif or "").strip():
        frappe.throw(_("Une annulation exige un motif."))
    doc = frappe.get_doc("Pointage Journalier", name)
    if doc.statut_validation == "Annulé":
        frappe.throw(_("{0} est déjà annulé.").format(name))
    doc.remarque = ((doc.remarque + "\n") if doc.remarque else "") + \
        _("Annulé : {0}").format(motif)
    doc.statut_validation = "Annulé"
    doc.save()
    return {"name": doc.name}
