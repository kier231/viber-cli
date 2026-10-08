'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => { const element = document.createElement(tag); if (text != null) element.textContent = text; if (className) element.className = className; return element; };
  const date = value => value ? new Date(value).toLocaleString() : '—';
  const showError = error => { $('error').textContent = error.message || 'The local request failed.'; $('error').hidden = false; };
  const clearError = () => { $('error').hidden = true; };
  const action = work => async event => { event?.preventDefault(); clearError(); try { await work(event); } catch (error) { showError(error); } };
  const views = ['compose', 'contacts', 'conversation', 'activity', 'health', 'events', 'sent', 'inbox'];
  const pendingKey = 'viberoutreach-pending-send';
  const draftKey = 'viberoutreach-draft';
  let csrf, contacts = [], view = 'compose', busy = false, preview = null, conversationLead = null, contactPage = 0;
  let offsets = { events: 0, sent: 0, inbox: 0 };
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem(pendingKey) || 'null'); }
  catch { showError(new Error('The saved send attempt cannot be read. Check Sent before sending another message.')); }

  async function api(path, payload) {
    const response = await fetch('/viber/api/' + path, { method: payload === undefined ? 'GET' : 'POST', credentials: 'same-origin', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-Viber-CSRF': csrf || '', ...(path === 'session' ? {'X-Viber-Browser': '1'} : {}) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }), signal: AbortSignal.timeout(15000) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || `Request failed (${response.status}).`);
    return result;
  }

  function setBusy(value, message = '') {
    busy = value;
    $('viber-busy').hidden = !value;
    $('viber-busy').textContent = message;
    for (const element of document.querySelectorAll('button, input, select, textarea')) {
      if (element.closest('.workspace-picker') || element.closest('nav') || element.id === 'sender') continue;
      element.disabled = value;
    }
    updateCompose();
    updateContactPagination();
  }

  async function operation(path, payload, message) {
    if (busy) throw new Error('Wait for the current Viber operation to finish.');
    setBusy(true, message);
    try {
      const task = await api(path, payload);
      return await waitOperation(task.operation_id);
    } finally { setBusy(false); }
  }

  async function waitOperation(id) {
    for (;;) {
      const result = await api('operations/' + encodeURIComponent(id));
      if (result.state === 'SUCCEEDED') return result.result;
      if (['FAILED', 'INTERRUPTED'].includes(result.state)) throw new Error(result.error || 'The operation stopped.');
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }

  function updateCompose() {
    $('compose-fields').disabled = busy || !!preview || !!pending;
    $('confirm-check').disabled = busy;
    $('confirm-cancel').disabled = busy;
    $('confirm-send').disabled = busy || !$('confirm-check').checked || !preview || preview.expires_at * 1000 <= Date.now();
    $('recover-send').hidden = !pending;
    $('recover-send').disabled = busy;
    $('new-message').disabled = busy || !!pending;
    $('confirmation').hidden = !preview;
  }

  function recipientDetail() {
    const lead = contacts.find(item => String(item.id) === $('to').value);
    $('recipient-detail').textContent = lead ? `${lead.phone} · Viber name: ${lead.viber_name || 'not discovered yet'} · ${lead.contact_name}` : 'Choose Contacts to add a number to your ledger.';
  }

  function saveDraft() {
    localStorage.setItem(draftKey, JSON.stringify({ lead_id: $('to').value, text: $('body').value }));
  }

  async function loadContacts() {
    const selected = $('to').value, readSelected = $('read-contact').value;
    contacts = await api('contacts');
    for (const [id, value] of [['to', selected], ['read-contact', readSelected]]) {
      const select = $(id); select.replaceChildren(node('option', 'Choose a saved contact')); select.options[0].value = '';
      for (const lead of contacts) { const option = node('option', `#${lead.id} · ${lead.company_name}${lead.viber_name ? ' · ' + lead.viber_name : ''} · ${lead.phone}`); option.value = lead.id; select.append(option); }
      select.value = value;
    }
    $('status').textContent = `${contacts.length} saved contact${contacts.length === 1 ? '' : 's'} · Viber Desktop · background mode`;
    recipientDetail(); renderContacts();
  }

  function selectLead(leadId) {
    if (pending || preview || busy) throw new Error('Finish or cancel the current message review before changing the recipient.');
    $('to').value = String(leadId); $('read-contact').value = String(leadId); recipientDetail(); saveDraft();
  }

  function contactButton(label, work) {
    const button = node('button', label); button.type = 'button'; button.disabled = busy; button.addEventListener('click', action(work)); return button;
  }

  function filteredContacts() {
    const search = $('contact-search').value.trim().toLocaleLowerCase();
    return contacts.filter(lead => [lead.id, lead.phone, lead.company_name, lead.viber_name || '', lead.contact_name].join(' ').toLocaleLowerCase().includes(search));
  }

  function updateContactPagination() {
    const total = filteredContacts().length;
    contactPage = Math.min(contactPage, Math.max(0, Math.ceil(total / 20) - 1));
    $('contact-prev').disabled = busy || contactPage === 0;
    $('contact-next').disabled = busy || (contactPage + 1) * 20 >= total;
    $('contact-page').textContent = `${total ? contactPage * 20 + 1 : 0}–${Math.min(total, (contactPage + 1) * 20)} of ${total}`;
  }

  function renderContacts() {
    updateContactPagination();
    $('contact-list').replaceChildren();
    const visible = filteredContacts().slice(contactPage * 20, (contactPage + 1) * 20);
    if (!visible.length) $('contact-list').append(node('p', 'No matching contacts.', 'viber-empty'));
    for (const lead of visible) {
      const record = node('div', null, 'contact-record'), row = node('div', null, 'contact-row'), text = node('div');
      text.append(node('strong', lead.company_name + (lead.viber_name ? ' · ' + lead.viber_name : '')),
        node('span', `#${lead.id} · ${lead.phone} · ${lead.contact_name}`));
      const actions = node('div', null, 'contact-actions');
      actions.append(contactButton('Message', async () => { selectLead(lead.id); await navigate('compose'); }),
        contactButton('Open', () => operation('open', { lead_id: lead.id }, 'Opening and verifying Viber in the background…')),
        contactButton('Read', async () => { const result = await operation('read', { lead_id: lead.id }, 'Reading the verified conversation…'); showConversation(result); await navigate('conversation'); }));
      row.append(text, actions); record.append(row);
      const editor = node('details', null, 'contact-editor'); editor.append(node('summary', 'Contact details and Viber name'));
      const grid = node('dl', null, 'contact-detail-grid');
      for (const [key, value] of [['Business', lead.company_name], ['Phone', lead.phone], ['Android contact', lead.contact_name], ['Created', date(lead.created_at)]]) grid.append(node('dt', key), node('dd', value));
      const form = node('form', null, 'contact-edit-form'), label = node('label', "Person's Viber name"), input = node('input');
      input.id = 'viber-name-' + lead.id; input.value = lead.viber_name || ''; input.maxLength = 120; input.required = true; label.htmlFor = input.id;
      const submit = node('button', 'Save Viber name'); submit.type = 'submit'; submit.disabled = busy;
      form.append(label, input, submit); form.addEventListener('submit', action(async () => {
        if (busy) throw new Error('Wait for the Viber operation to finish.');
        await api('name', { lead_id: lead.id, name: input.value });
        preview = null; updateCompose(); await loadContacts(); $('contact-status').textContent = 'Viber name saved. Business and Android contact names preserved.';
      }));
      editor.append(grid, form); record.append(editor); $('contact-list').append(record);
    }
  }

  function showConversation(result) {
    conversationLead = result.lead?.id || null;
    if (conversationLead) $('read-contact').value = String(conversationLead);
    $('conversation-meta').textContent = `${result.viber_name} · captured ${date(result.captured_at)}`;
    renderMessages($('conversation-messages'), result.messages || []);
    $('conversation-reply').hidden = !conversationLead;
  }

  function renderMessages(target, messages) {
    target.replaceChildren();
    if (!messages.length) target.append(node('p', 'Viber exposed no visible message text.', 'viber-empty'));
    for (const message of messages) {
      const record = node('div', null, 'viber-record'); record.append(node('strong', message.direction || 'MESSAGE'), node('pre', message.text)); target.append(record);
    }
  }

  async function loadRecords(kind, more = false) {
    if (!more) offsets[kind] = 0;
    const records = await api(kind + '?offset=' + offsets[kind]);
    const list = $(kind === 'events' ? 'event-list' : kind + '-list');
    if (!more) list.replaceChildren();
    if (!records.length && !more) list.append(node('p', kind === 'inbox' ? 'No conversation snapshots yet. Read a contact to save one.' : 'No recorded items yet.', 'viber-empty'));
    for (const item of records) {
      const record = node('details', null, 'viber-record');
      if (kind === 'sent') {
        record.append(node('summary', `${item.company_name} · ${item.viber_name} · ${item.state}`), node('p', `${item.phone} · ${date(item.created_at)}`, 'hint'), node('pre', item.text));
        if (item.error) record.append(node('p', item.error, 'warning'));
        else if (item.state === 'DISPATCHED') record.append(node('p', 'Send action dispatched; delivery is unverified.', 'hint'));
      } else if (kind === 'events') record.append(node('summary', `${date(item.created_at)} · ${item.kind}`), node('p', item.detail));
      else {
        record.append(node('summary', `${item.company_name} · ${item.viber_name}`), node('p', `Captured ${date(item.captured_at)}`, 'hint'));
        const messages = node('div'); renderMessages(messages, item.messages); record.append(messages);
        record.append(contactButton('Read again', async () => { const result = await operation('read', { lead_id: item.lead_id }, 'Refreshing the visible conversation…'); showConversation(result); await navigate('conversation'); }));
      }
      list.append(record);
    }
    offsets[kind] += records.length;
    $(kind === 'events' ? 'event-more' : kind + '-more').hidden = records.length < 100;
  }

  async function loadActivity() {
    const days = await api('activity?days=' + $('activity-days').value);
    for (const state of ['dispatched', 'blocked', 'unknown']) $('activity-' + state).textContent = days.reduce((sum, day) => sum + day[state], 0);
    $('activity-contacts').textContent = contacts.length;
    $('activity-rows').replaceChildren();
    for (const day of days) { const row = node('tr'); row.append(node('th', day.date), node('td', day.dispatched), node('td', day.blocked), node('td', day.unknown)); $('activity-rows').append(row); }
  }

  async function navigate(next) {
    view = next;
    for (const id of views) $(id).hidden = id !== next;
    document.querySelectorAll('[data-view]').forEach(button => { if (button.dataset.view === next) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    history.replaceState(null, '', '/viber#' + next);
    if (next === 'contacts') renderContacts();
    if (next === 'activity') await loadActivity();
    if (['events', 'sent', 'inbox'].includes(next)) await loadRecords(next);
  }

  async function checkHealth(inspect = false) {
    const result = await operation(inspect ? 'inspect' : 'diagnostics', {}, inspect ? 'Reading Viber controls without taking focus…' : 'Checking your local Viber setup…');
    $('health-checks').replaceChildren();
    for (const check of result.checks) {
      const card = node('div', null, 'health-card ' + (check.ok ? 'health-healthy' : 'health-unhealthy'));
      card.append(node('h3', check.name), node('p', (check.ok ? 'Ready · ' : 'Needs attention · ') + check.detail)); $('health-checks').append(card);
    }
    $('inspect-tree').hidden = !result.tree; $('inspect-tree').textContent = result.tree || '';
  }

  async function recoverSend() {
    if (!pending) return;
    setBusy(true, 'Checking the saved send attempt. This does not submit another message…');
    try {
      if (!pending.operation_id) {
        const task = await api('send', pending.payload); pending.operation_id = task.operation_id; localStorage.setItem(pendingKey, JSON.stringify(pending));
      }
      try {
        await waitOperation(pending.operation_id);
        $('send-result').textContent = 'Send action dispatched to Viber. Delivery is unverified. Open Sent for the attempt details.';
      } catch (error) {
        $('send-result').textContent = 'The attempt stopped. Review Sent and Viber before trying again.';
        throw error;
      } finally {
        // A known terminal operation can be cleared; a network failure stays saved.
        const state = await api('operations/' + encodeURIComponent(pending.operation_id));
        if (['SUCCEEDED', 'FAILED', 'INTERRUPTED'].includes(state.state)) { pending = null; localStorage.removeItem(pendingKey); preview = null; }
      }
    } finally { setBusy(false); }
  }

  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', action(() => navigate(button.dataset.view))));
  $('to').addEventListener('change', () => { preview = null; recipientDetail(); saveDraft(); updateCompose(); });
  $('body').addEventListener('input', () => { preview = null; saveDraft(); updateCompose(); });
  $('compose-form').addEventListener('submit', action(async () => {
    if (pending) throw new Error('Check the saved send attempt first.');
    preview = await operation('prepare', { lead_id: Number($('to').value), text: $('body').value }, 'Opening the dial pad and verifying this recipient. No message is sent during review…');
    $('confirm-recipient').replaceChildren();
    for (const [label, value] of [['Business', preview.lead.company_name], ['Viber name', preview.viber_name], ['Phone', preview.lead.phone], ['Android contact', preview.lead.contact_name]]) $('confirm-recipient').append(node('dt', label), node('dd', value));
    $('confirm-body').textContent = preview.text; $('confirm-check').checked = false;
    updateCompose(); await loadContacts(); $('confirmation').scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }));
  $('confirm-check').addEventListener('change', updateCompose);
  $('confirm-cancel').addEventListener('click', () => { preview = null; updateCompose(); });
  $('confirm-send').addEventListener('click', action(async () => {
    if (!preview || !$('confirm-check').checked || preview.expires_at * 1000 <= Date.now()) throw new Error('Review this message again before sending.');
    pending = { payload: { preview_token: preview.token, confirmed: true, request_key: crypto.randomUUID() } };
    localStorage.setItem(pendingKey, JSON.stringify(pending));
    await recoverSend();
  }));
  $('recover-send').addEventListener('click', action(recoverSend));
  $('new-message').addEventListener('click', () => { preview = null; $('body').value = ''; $('send-result').textContent = ''; saveDraft(); updateCompose(); });
  $('compose-open').addEventListener('click', action(async () => { await operation('open', { lead_id: Number($('to').value) }, 'Opening and verifying this number in the background…'); await loadContacts(); $('send-result').textContent = 'Conversation opened without taking Windows focus.'; }));
  $('contact-form').addEventListener('submit', action(async () => {
    const result = await operation('contacts', { phone: $('contact-phone').value, company_name: $('contact-company').value }, 'Adding and verifying the Android contact…');
    $('contact-form').reset(); $('add-panel').open = false; await loadContacts(); $('contact-status').textContent = `Added #${result.id}: ${result.contact_name}.`;
  }));
  $('contact-search').addEventListener('input', () => { contactPage = 0; renderContacts(); });
  $('contact-prev').addEventListener('click', () => { contactPage--; renderContacts(); });
  $('contact-next').addEventListener('click', () => { contactPage++; renderContacts(); });
  $('read-form').addEventListener('submit', action(async () => { const result = await operation('read', { lead_id: Number($('read-contact').value) }, 'Reading this verified conversation…'); await loadContacts(); showConversation(result); }));
  $('read-current').addEventListener('click', action(async () => { const result = await operation('read-current', {}, 'Verifying and reading the current Viber chat…'); showConversation(result); await navigate('conversation'); }));
  $('conversation-reply').addEventListener('click', action(async () => { selectLead(conversationLead); await navigate('compose'); }));
  $('activity-form').addEventListener('submit', action(loadActivity));
  $('check-health').addEventListener('click', action(() => checkHealth()));
  $('inspect').addEventListener('click', action(() => checkHealth(true)));
  for (const kind of ['sent', 'events', 'inbox']) $(kind === 'events' ? 'event-more' : kind + '-more').addEventListener('click', action(() => loadRecords(kind, true)));
  $('refresh').addEventListener('click', action(async () => { await loadContacts(); if (view === 'health') await checkHealth(); else await navigate(view); }));
  window.addEventListener('storage', event => {
    if (event.key === pendingKey) { try { pending = JSON.parse(event.newValue || 'null'); updateCompose(); } catch (error) { showError(error); } }
  });
  setInterval(() => { if (preview) updateCompose(); }, 1000);
  action(async () => {
    const session = await api('session', {}); csrf = session.csrf; await loadContacts();
    try { const draft = JSON.parse(localStorage.getItem(draftKey) || 'null'); if (draft) { $('to').value = draft.lead_id || ''; $('body').value = draft.text || ''; recipientDetail(); } } catch { /* A broken unsent draft can be rewritten. */ }
    updateCompose();
    const requested = location.hash.slice(1); await navigate(views.includes(requested) ? requested : 'compose');
    if (pending) $('send-result').textContent = 'A send attempt is saved. Check its status before composing another message.';
  })();
})();
