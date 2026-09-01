# -*- coding: utf-8 -*-
"""Caisse hiérarchique LEH — contrôleurs Caisse/Mouvement Caisse + API dashboard.

Modèle (mission Tarak 31/08, contre-audit Kimi GO-avec-corrections appliqué) :
- Caisse devises (USD/EUR) : reçoit les fonds ; conversion vers LYD, CHAQUE
  conversion porte son taux (taux_change obligatoire).
- Caisse LYD principale : dépenses chantier, alimentée par les conversions.
- Sous-caisses (profondeur 1, même devise que le parent) : les dépenses d'un
  caissier naissent "En attente" ; le chef de projet valide à l'unité ou en
  lot -> remontent dans la vue consolidée. Remise à niveau EXPLICITE
  (recharge au fond / vidage), refusée tant qu'il reste de l'En attente.
- La validation ne bouge JAMAIS le cash : solde = réalité physique.
- Pas d'écriture GL (Règle B suspendue pour la caisse chantier — décision
  Tarak 30/08 « l'ERP ne gère pas la compta officielle » ; passerelle possible
  plus tard via le Link Account des fiches Caisse).

Sécurité (Kimi C1/C7) : les champs caisse/caisse_cible portent
ignore_user_permissions=1 ; TOUT le contrôle d'accès passe par
mc_query_conditions / mc_has_permission (OR sur caisse|caisse_cible en
lecture pour que le caissier voie les recharges qu'il reçoit) + la matrice
type->rôle dans validate (un caissier ne crée QUE des Dépenses, uniquement
sur ses caisses permises).

Sémantique de solde d'une caisse C (à la volée, jamais stocké ; statut
'Annulé' exclu) :
  + Entrée(C) + montant_cible des Transfert/Conversion dont caisse_cible=C
  - Dépense(C) - Transfert(C) - Conversion(C)
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

VALIDATOR_ROLES = {"System Manager", "ETRAPH Chef de Projet"}
TYPES = ("Entrée", "Dépense", "Transfert", "Conversion")


def _is_validator(user=None):
    return bool(set(frappe.get_roles(user or frappe.session.user)) & VALIDATOR_ROLES)


def _permitted_caisses(user=None):
    """Caisses autorisées par User Permission (vide = aucune)."""
    user = user or frappe.session.user
    perms = frappe.permissions.get_user_permissions(user=user)
    return {p.get("doc") for p in (perms.get("Caisse") or [])}


# ------------------------------------------------------------------ Caisse

def validate_caisse(doc, method=None):
    """Kimi C3 : hiérarchie bornée (profondeur 1, pas de cycle, devise=parent)."""
    if doc.caisse_parent:
        if doc.caisse_parent == doc.name:
            frappe.throw(_("Une caisse ne peut pas être sa propre parente."))
        parent = frappe.get_doc("Caisse", doc.caisse_parent)
        if parent.caisse_parent:
            frappe.throw(_("Hiérarchie limitée à un niveau : {0} est déjà une "
                           "sous-caisse.").format(doc.caisse_parent))
        if parent.devise and doc.devise and parent.devise != doc.devise:
            frappe.throw(_("Une sous-caisse a la même devise que sa caisse "
                           "parente ({0}).").format(parent.devise))
        doc.devise = parent.devise or doc.devise
    if not doc.is_new():
        enfants = frappe.get_all("Caisse", filters={"caisse_parent": doc.name}, pluck="name")
        if enfants and doc.caisse_parent:
            frappe.throw(_("{0} a des sous-caisses ({1}) : elle ne peut pas devenir "
                           "elle-même une sous-caisse.").format(doc.name, ", ".join(enfants)))


# ------------------------------------------------- Mouvement Caisse controller

def validate_mouvement(doc, method=None):
    if getattr(frappe.flags, "in_import_caisse", False):
        return  # import historique : cohérence contrôlée par le script (totaux recalculés)

    is_val = _is_validator()

    if doc.type_mouvement not in TYPES:
        frappe.throw(_("Type de mouvement inconnu : {0}").format(doc.type_mouvement))
    if flt(doc.montant) <= 0:
        frappe.throw(_("Le montant doit être strictement positif."))

    caisse = frappe.get_doc("Caisse", doc.caisse)
    if not caisse.actif:
        frappe.throw(_("La caisse {0} est inactive.").format(doc.caisse))
    doc.devise = caisse.devise
    doc.projet = caisse.projet

    # Kimi C1 — matrice type -> rôle + périmètre du caissier
    if not is_val:
        if doc.type_mouvement != "Dépense":
            frappe.throw(_("Un caissier ne saisit que des Dépenses. Les entrées, "
                           "transferts et conversions sont réservés au chef de projet."))
        allowed = _permitted_caisses()
        if doc.caisse not in allowed:
            frappe.throw(_("Vous n'êtes pas autorisé à écrire dans la caisse {0}.")
                         .format(doc.caisse), frappe.PermissionError)

    if doc.type_mouvement in ("Transfert", "Conversion"):
        if not doc.caisse_cible:
            frappe.throw(_("Caisse cible obligatoire pour un {0}.").format(doc.type_mouvement))
        if doc.caisse_cible == doc.caisse:
            frappe.throw(_("La caisse cible doit être différente de la caisse source."))
        cible = frappe.get_doc("Caisse", doc.caisse_cible)
        if not cible.actif:
            frappe.throw(_("La caisse cible {0} est inactive.").format(doc.caisse_cible))
        doc.devise_cible = cible.devise
        if doc.type_mouvement == "Transfert":
            if cible.devise != caisse.devise:
                frappe.throw(_("Un transfert relie deux caisses de même devise "
                               "(utiliser une Conversion pour changer de devise)."))
            doc.taux_change = None
            doc.montant_cible = flt(doc.montant)
        else:  # Conversion
            if cible.devise == caisse.devise:
                frappe.throw(_("Une conversion relie deux caisses de devises différentes."))
            if flt(doc.taux_change) <= 0:
                frappe.throw(_("Le taux de change est obligatoire pour chaque conversion "
                               "(règle ETRAPH : chaque opération porte son taux)."))
            # Kimi C5 : montant_cible ajustable (arrondi négocié, commission) ;
            # défaut = montant x taux, écart accepté mais tracé.
            theorique = flt(flt(doc.montant) * flt(doc.taux_change), 3)
            if not flt(doc.montant_cible):
                doc.montant_cible = theorique
            elif abs(flt(doc.montant_cible) - theorique) > max(0.02 * theorique, 1):
                frappe.msgprint(_("Écart notable entre le montant crédité ({0}) et "
                                  "montant × taux ({1}) — vérifier le taux ou la remarque.")
                                .format(doc.montant_cible, theorique), indicator="orange")
    else:
        doc.caisse_cible = None
        doc.taux_change = None
        doc.montant_cible = None
        doc.devise_cible = None

    _apply_statut_rules(doc, is_val)

    # Découvert : non bloquant (l'imprest réel tolère des avances) — signal seulement.
    if doc.type_mouvement in ("Dépense", "Transfert", "Conversion"):
        s = solde_caisse(doc.caisse)
        delta = flt(doc.montant) if doc.is_new() else 0
        if doc.is_new() and s - delta < 0:
            frappe.msgprint(_("Attention : ce mouvement met la caisse {0} en négatif "
                              "({1} {2}).").format(doc.caisse, flt(s - delta, 3), doc.devise),
                            indicator="orange")


def _apply_statut_rules(doc, is_val):
    """Kimi C2 : statut par type + règles de transition (tracées en Version)."""
    if doc.is_new():
        if doc.type_mouvement == "Dépense" and not is_val:
            doc.statut_validation = "En attente"
        else:
            doc.statut_validation = "Validé"
            doc.valide_par = frappe.session.user if is_val else None
            doc.date_validation = now_datetime() if is_val else None
        return

    old = doc.get_doc_before_save()
    if not old:
        return
    old_st, new_st = old.statut_validation, doc.statut_validation

    if old.importe_excel and not is_val:
        frappe.throw(_("Mouvement importé de l'historique Excel : lecture seule."))

    if old_st != new_st:
        allowed = {("En attente", "Validé"), ("Validé", "Annulé"), ("En attente", "Annulé")}
        if (old_st, new_st) not in allowed:
            frappe.throw(_("Transition de statut {0} → {1} interdite.").format(old_st, new_st))
        if not is_val:
            frappe.throw(_("Seul le chef de projet peut changer le statut de validation."))
        if new_st == "Annulé" and not (doc.remarque or "").strip():
            frappe.throw(_("Une annulation exige une remarque (motif)."))
        if new_st == "Validé":
            doc.valide_par = frappe.session.user
            doc.date_validation = now_datetime()
    elif old_st in ("Validé", "Annulé") and not is_val:
        frappe.throw(_("Ce mouvement est {0} : il ne peut plus être modifié "
                       "(demander au chef de projet).").format(old_st.lower()))


def on_trash_mouvement(doc, method=None):
    if getattr(frappe.flags, "in_import_caisse", False):
        return
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Un mouvement ne se supprime pas : il s'ANNULE (statut Annulé, "
                       "motif en remarque). Suppression réservée à l'administrateur."))


# ---------------------------------------------- accès (Kimi C1/C7, OR lecture)

def mc_query_conditions(user):
    """permission_query_conditions Mouvement Caisse : le caissier voit les
    mouvements dont SA caisse est source OU cible (recharges reçues incluses)."""
    user = user or frappe.session.user
    if _is_validator(user):
        return ""
    allowed = _permitted_caisses(user)
    if not allowed:
        return "1=0"
    esc = ", ".join(frappe.db.escape(c) for c in allowed)
    return ("(`tabMouvement Caisse`.caisse in ({0}) or "
            "`tabMouvement Caisse`.caisse_cible in ({0}))").format(esc)


def mc_has_permission(doc, ptype, user):
    user = user or frappe.session.user
    if _is_validator(user):
        return True
    allowed = _permitted_caisses(user)
    if ptype in ("read", "print", "email", "select"):
        return doc.caisse in allowed or (doc.caisse_cible and doc.caisse_cible in allowed)
    return doc.caisse in allowed


# ---------------------------------------------------------------- soldes

def solde_caisse(caisse):
    rows = frappe.db.sql(
        """
        select
          coalesce(sum(case when caisse=%(c)s and type_mouvement='Entrée' then montant else 0 end),0)
          - coalesce(sum(case when caisse=%(c)s and type_mouvement in ('Dépense','Transfert','Conversion') then montant else 0 end),0)
          + coalesce(sum(case when caisse_cible=%(c)s and type_mouvement in ('Transfert','Conversion') then coalesce(montant_cible,0) else 0 end),0)
        from `tabMouvement Caisse`
        where (caisse=%(c)s or caisse_cible=%(c)s)
          and coalesce(statut_validation,'') != 'Annulé'
        """,
        {"c": caisse},
    )
    return flt(rows[0][0] if rows else 0, 3)


@frappe.whitelist()
def get_dashboard():
    """Dashboard Caisse LEH. Kimi C7 : rôle exigé + périmètre filtré."""
    roles = set(frappe.get_roles())
    if not (roles & (VALIDATOR_ROLES | {"ETRAPH Caissier"})):
        frappe.throw(_("Accès réservé aux rôles caisse ETRAPH."), frappe.PermissionError)
    peut_valider = _is_validator()
    filters = {"actif": 1}
    caisses = frappe.get_all(
        "Caisse", filters=filters,
        fields=["name", "caisse_name", "devise", "caisse_parent",
                "fond_de_caisse", "responsable", "projet"],
        order_by="caisse_parent asc, name asc",
    )
    if not peut_valider:
        allowed = _permitted_caisses()
        caisses = [c for c in caisses if c.name in allowed]
    for c in caisses:
        pend = frappe.get_all(
            "Mouvement Caisse",
            filters={"caisse": c.name, "statut_validation": "En attente"},
            fields=["name", "date", "montant", "designation", "beneficiaire", "owner"],
            order_by="date asc, creation asc",
        )
        c["solde"] = solde_caisse(c.name)
        c["en_attente"] = pend
        c["total_en_attente"] = flt(sum(flt(p.montant) for p in pend), 3)
        c["responsable_nom"] = frappe.db.get_value("User", c.responsable, "full_name") \
            if c.get("responsable") else None
    return {"caisses": caisses, "peut_valider": peut_valider, "user": frappe.session.user}


@frappe.whitelist()
def get_dashboard_site(projet):
    """Variante de get_dashboard() filtrée par chantier (multi-site : LEH, ARP...).
    Additive uniquement — get_dashboard() reste inchangée pour ne rien casser
    sur le bloc Caisse LEH existant."""
    roles = set(frappe.get_roles())
    if not (roles & (VALIDATOR_ROLES | {"ETRAPH Caissier"})):
        frappe.throw(_("Accès réservé aux rôles caisse ETRAPH."), frappe.PermissionError)
    peut_valider = _is_validator()
    caisses = frappe.get_all(
        "Caisse", filters={"actif": 1, "projet": projet},
        fields=["name", "caisse_name", "devise", "caisse_parent",
                "fond_de_caisse", "responsable", "projet"],
        order_by="caisse_parent asc, name asc",
    )
    if not peut_valider:
        allowed = _permitted_caisses()
        caisses = [c for c in caisses if c.name in allowed]
    for c in caisses:
        pend = frappe.get_all(
            "Mouvement Caisse",
            filters={"caisse": c.name, "statut_validation": "En attente"},
            fields=["name", "date", "montant", "designation", "beneficiaire", "owner"],
            order_by="date asc, creation asc",
        )
        c["solde"] = solde_caisse(c.name)
        c["en_attente"] = pend
        c["total_en_attente"] = flt(sum(flt(p.montant) for p in pend), 3)
        c["responsable_nom"] = frappe.db.get_value("User", c.responsable, "full_name") \
            if c.get("responsable") else None
    return {"caisses": caisses, "peut_valider": peut_valider, "user": frappe.session.user}


# ---------------------------------------------------------------- validation

def _assert_validator():
    if not _is_validator():
        frappe.throw(_("Action réservée au chef de projet (rôle ETRAPH Chef de Projet)."),
                     frappe.PermissionError)


def _valider_un(name):
    """Kimi C2 : via doc.save() (Version tracée), jamais db_set."""
    doc = frappe.get_doc("Mouvement Caisse", name)
    if doc.statut_validation != "En attente":
        frappe.throw(_("{0} n'est pas en attente (statut : {1}).")
                     .format(name, doc.statut_validation))
    doc.statut_validation = "Validé"
    doc.save()
    return doc


@frappe.whitelist()
def valider_mouvement(name):
    _assert_validator()
    doc = _valider_un(name)
    return {"name": doc.name, "caisse": doc.caisse, "montant": doc.montant}


@frappe.whitelist()
def valider_lot(caisse):
    """Valide les DÉPENSES en attente de cette caisse uniquement (Kimi C2)."""
    _assert_validator()
    pend = frappe.get_all("Mouvement Caisse",
                          filters={"caisse": caisse, "statut_validation": "En attente",
                                   "type_mouvement": "Dépense"},
                          pluck="name")
    total = 0.0
    for name in pend:
        total += flt(_valider_un(name).montant)
    return {"caisse": caisse, "valides": len(pend), "total": flt(total, 3)}


@frappe.whitelist()
def annuler_mouvement(name, motif):
    _assert_validator()
    if not (motif or "").strip():
        frappe.throw(_("Une annulation exige un motif."))
    doc = frappe.get_doc("Mouvement Caisse", name)
    if doc.statut_validation == "Annulé":
        frappe.throw(_("{0} est déjà annulé.").format(name))
    doc.statut_validation = "Annulé"
    doc.remarque = ((doc.remarque + "\n") if doc.remarque else "") + _("Annulé : {0}").format(motif)
    doc.save()
    return {"name": doc.name}


@frappe.whitelist()
def recharger_sous_caisse(caisse, mode="fond", montant_cible=None):
    """Remise à niveau explicite. Kimi C8 : refusée tant qu'il reste de
    l'En attente (valider ou annuler d'abord — sinon Aymen finance des
    dépenses non validées).

    mode='fond'  : transfert parent->sous jusqu'au fond de caisse (ou montant_cible).
    mode='vider' : transfert sous->parent du solde restant (repart à 0).
    """
    _assert_validator()
    c = frappe.get_doc("Caisse", caisse)
    if not c.caisse_parent:
        frappe.throw(_("{0} n'est pas une sous-caisse.").format(caisse))
    pend = frappe.db.count("Mouvement Caisse",
                           {"caisse": caisse, "statut_validation": "En attente"})
    if pend:
        frappe.throw(_("{0} dépense(s) encore en attente sur {1} : valider (ou annuler) "
                       "d'abord, puis recharger.").format(pend, caisse))
    solde = solde_caisse(caisse)
    if mode == "vider":
        if solde <= 0:
            frappe.throw(_("Solde {0} : rien à reverser.").format(solde))
        doc = frappe.get_doc({
            "doctype": "Mouvement Caisse", "type_mouvement": "Transfert",
            "caisse": caisse, "caisse_cible": c.caisse_parent,
            "montant": solde, "date": frappe.utils.today(),
            "designation": _("Vidage sous-caisse (retour grande caisse)"),
        }).insert()
        return {"mouvement": doc.name, "montant": solde,
                "nouveau_solde": solde_caisse(caisse)}
    cible = flt(montant_cible) if montant_cible else flt(c.fond_de_caisse)
    if cible <= 0:
        frappe.throw(_("Fond de caisse non défini sur {0} (et pas de montant cible fourni).")
                     .format(caisse))
    manque = flt(cible - solde, 3)
    if manque <= 0:
        frappe.throw(_("Solde actuel {0} ≥ cible {1} : rien à recharger.").format(solde, cible))
    doc = frappe.get_doc({
        "doctype": "Mouvement Caisse", "type_mouvement": "Transfert",
        "caisse": c.caisse_parent, "caisse_cible": caisse,
        "montant": manque, "date": frappe.utils.today(),
        "designation": _("Recharge sous-caisse au fond de caisse ({0})").format(cible),
    }).insert()
    return {"mouvement": doc.name, "montant": manque,
            "nouveau_solde": solde_caisse(caisse)}
