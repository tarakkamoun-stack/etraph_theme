// Dashboard Caisse LEH — Custom HTML Block (root_element = bloc racine)
// Palette identité chiffrage : vert #064f26 / or #e0c800
(function () {
  const main = root_element.querySelector('#cl-main');
  const fmt = (v, cur) =>
    (new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 0, maximumFractionDigits: 3 }).format(v || 0)) +
    ' ' + (cur || '');

  function esc(s) { return frappe.utils.escape_html(s || ''); }

  function load() {
    frappe.call('etraph_theme.caisse_leh.get_dashboard').then(r => render(r.message));
  }

  function carte(c, data, big) {
    const attente = c.en_attente || [];
    const peut = data.peut_valider;
    let rows = '';
    if (attente.length) {
      rows = attente.map(p => `
        <div class="cl-pend-row" data-name="${esc(p.name)}">
          <span class="cl-pend-date">${frappe.datetime.str_to_user(p.date) || ''}</span>
          <span class="cl-pend-des">${esc(p.designation)}</span>
          <span class="cl-pend-mt">${fmt(p.montant, c.devise)}</span>
          ${peut ? `<button class="cl-btn cl-btn-mini cl-valider-un" data-name="${esc(p.name)}">Valider</button>` : ''}
        </div>`).join('');
    }
    return `
      <div class="cl-card ${big ? 'cl-card-big' : 'cl-card-sub'}" data-caisse="${esc(c.name)}">
        <div class="cl-card-head">
          <span class="cl-card-name">${esc(c.caisse_name)}</span>
          <span class="cl-card-devise">${esc(c.devise)}</span>
        </div>
        <div class="cl-solde">${fmt(c.solde, c.devise)}</div>
        ${c.responsable_nom ? `<div class="cl-resp">Responsable : ${esc(c.responsable_nom)}</div>` : ''}
        ${c.fond_de_caisse ? `<div class="cl-fond">Fond de caisse : ${fmt(c.fond_de_caisse, c.devise)}</div>` : ''}
        ${attente.length ? `
          <div class="cl-pend">
            <div class="cl-pend-title">${attente.length} dépense(s) en attente — ${fmt(c.total_en_attente, c.devise)}</div>
            ${rows}
            ${peut ? `<div class="cl-actions">
              <button class="cl-btn cl-valider-lot" data-caisse="${esc(c.name)}">✔ Tout valider</button>
            </div>` : ''}
          </div>` : (c.caisse_parent ? `<div class="cl-pend-none">Aucune dépense en attente</div>` : '')}
        ${(peut && c.caisse_parent) ? `<div class="cl-actions cl-actions-bas">
            <button class="cl-btn cl-btn-ghost cl-recharger" data-caisse="${esc(c.name)}">Recharger au fond</button>
            <button class="cl-btn cl-btn-ghost cl-vider" data-caisse="${esc(c.name)}">Vider vers la grande caisse</button>
          </div>` : ''}
        <div class="cl-links">
          <a href="/app/mouvement-caisse/new?caisse=${encodeURIComponent(c.name)}&type_mouvement=Dépense">+ Dépense</a>
          <a href="/app/mouvement-caisse?caisse=${encodeURIComponent(c.name)}">Journal</a>
          ${!c.caisse_parent && c.devise !== 'LYD' ? `<a href="/app/mouvement-caisse/new?caisse=${encodeURIComponent(c.name)}&type_mouvement=Conversion&caisse_cible=Caisse%20LEH%20LYD">Convertir → LYD</a>` : ''}
          ${!c.caisse_parent && c.devise !== 'LYD' ? `<a href="/app/mouvement-caisse/new?caisse=${encodeURIComponent(c.name)}&type_mouvement=Entrée">+ Réception</a>` : ''}
        </div>
      </div>`;
  }

  function render(data) {
    const caisses = data.caisses || [];
    const grandes = caisses.filter(c => !c.caisse_parent);
    const sous = caisses.filter(c => c.caisse_parent);
    main.innerHTML = `
      <div class="cl-rangee cl-grandes">${grandes.map(c => carte(c, data, true)).join('')}</div>
      ${sous.length ? `<div class="cl-fleche">⬐ sous-caisses rattachées à la caisse LYD ⬎</div>` : ''}
      <div class="cl-rangee cl-sous">${sous.map(c => carte(c, data, false)).join('')}</div>`;

    main.querySelectorAll('.cl-valider-un').forEach(b => b.addEventListener('click', () => {
      frappe.call('etraph_theme.caisse_leh.valider_mouvement', { name: b.dataset.name })
        .then(() => { frappe.show_alert({ message: 'Dépense validée', indicator: 'green' }); load(); });
    }));
    main.querySelectorAll('.cl-valider-lot').forEach(b => b.addEventListener('click', () => {
      frappe.confirm('Valider TOUTES les dépenses en attente de ' + b.dataset.caisse + ' ?', () => {
        frappe.call('etraph_theme.caisse_leh.valider_lot', { caisse: b.dataset.caisse })
          .then(r => { frappe.show_alert({ message: r.message.valides + ' dépense(s) validée(s)', indicator: 'green' }); load(); });
      });
    }));
    main.querySelectorAll('.cl-recharger').forEach(b => b.addEventListener('click', () => {
      frappe.confirm('Recharger ' + b.dataset.caisse + ' à son fond de caisse (transfert depuis la grande caisse) ?', () => {
        frappe.call('etraph_theme.caisse_leh.recharger_sous_caisse', { caisse: b.dataset.caisse, mode: 'fond' })
          .then(r => { frappe.show_alert({ message: 'Rechargée (+' + r.message.montant + ')', indicator: 'green' }); load(); });
      });
    }));
    main.querySelectorAll('.cl-vider').forEach(b => b.addEventListener('click', () => {
      frappe.confirm('Reverser tout le solde de ' + b.dataset.caisse + ' vers la grande caisse (repart à 0) ?', () => {
        frappe.call('etraph_theme.caisse_leh.recharger_sous_caisse', { caisse: b.dataset.caisse, mode: 'vider' })
          .then(r => { frappe.show_alert({ message: 'Vidée (−' + r.message.montant + ')', indicator: 'green' }); load(); });
      });
    }));
  }

  load();
})();
