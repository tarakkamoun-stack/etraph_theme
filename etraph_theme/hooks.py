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
    }
}

# Synchro horaire desactivee tant que les 2 endpoints PHP cote Plesk
# n'existent pas (cf ETRAPH/site-carrieres/data/spec-endpoint-erp-sync.md).
# Fonction prete et testable manuellement : etraph_theme.candidature_sync.
# A activer en decommentant la ligne ci-dessous une fois les endpoints
# livres et le token pose dans Candidature Sync Settings.
# scheduler_events = {
#     "hourly": [
#         "etraph_theme.candidature_sync.sync_candidatures",
#     ]
# }
