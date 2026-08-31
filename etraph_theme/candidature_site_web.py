# Controller "custom" pour le DocType custom=1 "Candidature Site Web".
#
# Ce DocType est un MIROIR en lecture seule du portail carrieres
# (etraph.com/careers). Le sens de synchro est source -> ERP uniquement
# (D1-D6, avis Kimi 31/08). Aucune ecriture manuelle n'est autorisee,
# meme par un System Manager ou l'Administrator : seule la fonction de
# synchro (candidature_sync.sync_candidatures), qui pose
# frappe.flags.in_sync = True le temps du batch, peut creer/modifier/
# supprimer ces documents.
#
# Branche via hooks.py -> doc_events["Candidature Site Web"].
import frappe

READ_ONLY_MESSAGE = (
    "Candidature Site Web est un miroir en lecture seule du portail "
    "carrieres (etraph.com/careers). Toute correction doit se faire "
    "cote source ; ce document sera ecrase au prochain cycle de "
    "synchronisation horaire."
)


def guard_write(doc, method):
    # Couvre "validate" (donc insert() ET save()) et "before_insert".
    if not frappe.flags.get("in_sync"):
        frappe.throw(READ_ONLY_MESSAGE, frappe.PermissionError)


def guard_delete(doc, method):
    # Couvre "on_trash". La purge par absence (D4) est faite par la
    # synchro elle-meme (qui pose in_sync=True autour de la suppression),
    # jamais par un utilisateur via le Desk.
    if not frappe.flags.get("in_sync"):
        frappe.throw(READ_ONLY_MESSAGE, frappe.PermissionError)
