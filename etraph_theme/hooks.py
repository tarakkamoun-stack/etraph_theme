app_name = "etraph_theme"
app_title = "ETRAPH Theme"
app_publisher = "ETRAPH"
app_description = "Habillage ETRAPH (couleurs, polices, logo) pour ERPNext — pas de refonte de layout, uniquement des surcharges CSS pour survivre aux mises a jour."
app_email = "tk@etraph.com"
app_license = "MIT"

# Inclut le CSS de marque dans le Desk (ERPNext/Frappe apres connexion)
app_include_css = "/assets/etraph_theme/css/etraph_theme.css"

# Inclut le meme CSS sur les pages publiques (page de connexion, portail web)
web_include_css = "/assets/etraph_theme/css/etraph_theme.css"

# Candidature Site Web (miroir lecture seule du portail carrieres,
# etraph.com/careers) : refuse toute ecriture/suppression manuelle, sauf
# pendant le batch de synchro (frappe.flags.in_sync). Cf.
# etraph_theme/candidature_site_web.py.
doc_events = {
    "Candidature Site Web": {
        "validate": "etraph_theme.candidature_site_web.guard_write",
        "before_insert": "etraph_theme.candidature_site_web.guard_write",
        "on_trash": "etraph_theme.candidature_site_web.guard_delete",
    },
    # Devis Chiffrage : miroir lecture seule de chiffrage.etraph.com
    # (doctrine Kimi 30/08, sens unique source -> ERP). Cf.
    # etraph_theme/devis_chiffrage_sync.py.
    "Devis Chiffrage": {
        "validate": "etraph_theme.devis_chiffrage_sync.guard_write",
        "before_insert": "etraph_theme.devis_chiffrage_sync.guard_write",
        "on_trash": "etraph_theme.devis_chiffrage_sync.guard_delete",
    },
}

# Synchro carrieres : ACTIVE (31/08, endpoints api/erp-sync/ livres cote
# Plesk, token pose dans Candidature Sync Settings). Pull horaire, sens
# unique source -> ERP (etraph_theme/candidature_sync.py).
#
# Synchro Devis Chiffrage : ACTIVE (31/08, approbation Tarak en session
# principale). Pull horaire depuis chiffrage.etraph.com, config dans le
# Single "Devis Chiffrage Sync Settings".
scheduler_events = {
    "hourly": [
        "etraph_theme.devis_chiffrage_sync.sync_devis",
        "etraph_theme.candidature_sync.sync_candidatures",
    ]
}
