'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => { const element = document.createElement(tag); if (text != null) element.textContent = text; if (className) element.className = className; return element; };
  const timeZone = 'Europe/Warsaw';
  const date = value => value ? new Date(value).toLocaleString(undefined, {timeZone, timeZoneName: 'short'}) : '—';
  const localDateTime = value => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {timeZone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).formatToParts(value).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  };
  const showError = error => { $('error').textContent = error.message || 'The local request failed.'; $('error').hidden = false; };
  const clearError = () => { $('error').hidden = true; };
  const action = work => async event => { event?.preventDefault(); clearError(); try { await work(event); } catch (error) { showError(error); } };
  const views = ['compose', 'scheduled', 'campaigns', 'contacts', 'conversation', 'activity', 'health', 'events', 'sent', 'inbox'];
  const pendingKey = 'viberoutreach-pending-send';
  const draftKey = 'viberoutreach-draft';
  const campaignPendingKey = 'viberoutreach-pending-campaign';
  const campaignSelection = new Set();
  let campaignPreview = null, campaignPending = null, campaignWorking = false, campaignsLoading = false;
  let campaignsSignature = null;
  let csrf, contacts = [], view = 'compose', busy = false, preview = null, conversationLead = null, contactPage = 0;
  let offsets = { events: 0, sent: 0, inbox: 0, scheduled: 0 };
  let schedulesLoading = false;
  let databaseLoading = false, databaseOffset = 0, databaseSignature = null;
  let databaseChat = null, databaseOlder = null, databaseRevision = null;
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem(pendingKey) || 'null'); }
  catch { showError(new Error('The saved send attempt cannot be read. Check Sent before sending another message.')); }
  try { campaignPending = JSON.parse(localStorage.getItem(campaignPendingKey) || 'null'); }
  catch { showError(new Error('The saved campaign submission cannot be read. Check Your campaigns before creating another.')); }

  async function api(path, payload) {
    const response = await fetch('/viber/api/' + path, { method: payload === undefined ? 'GET' : 'POST', credentials: 'same-origin', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-Viber-CSRF': csrf || '', ...(path === 'session' ? {'X-Viber-Browser': '1'} : {}) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }), signal: AbortSignal.timeout(15000) });
    const result = await response.json();
    if (!response.ok) { const error = new Error(result.message || `Request failed (${response.status}).`); error.status = response.status; throw error; }
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
    updateCampaignControls();
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
    $('confirm-send').disabled = busy || !$('confirm-check').checked || !preview || preview.expires_at * 1000 <= Date.now() || (preview.scheduled_at && new Date(preview.scheduled_at).getTime() <= Date.now());
    $('confirm-send').textContent = preview?.scheduled_at ? 'Schedule message' : 'Send message';
    $('send').textContent = $('schedule-enabled').checked ? 'Review scheduled message' : 'Review message';
    $('recover-send').hidden = !pending;
    $('recover-send').disabled = busy;
    $('new-message').disabled = busy || !!pending;
    $('confirmation').hidden = !preview;
    setScheduleState();
  }

  function setScheduleState() {
    const enabled = $('schedule-enabled').checked;
    const delay = $('schedule-mode').value === 'delay';
    const locked = busy || !!preview || !!pending;
    $('schedule-options').hidden = !enabled;
    $('schedule-mode').disabled = locked || !enabled;
    $('schedule-time-wrap').hidden = delay;
    $('schedule-delay-wrap').hidden = !delay;
    $('schedule-at').disabled = locked || !enabled || delay;
    $('schedule-at').required = enabled && !delay;
    $('schedule-delay-minutes').disabled = locked || !enabled || !delay;
    $('schedule-delay-minutes').required = enabled && delay;
  }

  function schedulePayload() {
    if (!$('schedule-enabled').checked) return undefined;
    if ($('schedule-mode').value === 'delay') return {mode: 'delay', minutes: Number($('schedule-delay-minutes').value)};
    return {mode: 'datetime', local_time: $('schedule-at').value, time_zone: timeZone};
  }

  function recipientDetail() {
    const lead = contacts.find(item => String(item.id) === $('to').value);
    $('recipient-detail').textContent = lead ? `${lead.phone} · Viber name: ${lead.viber_name || 'not discovered yet'} · ${lead.contact_name}` : 'Choose Contacts to add a number to your ledger.';
  }

  function saveDraft() {
    localStorage.setItem(draftKey, JSON.stringify({ lead_id: $('to').value, text: $('body').value,
      schedule_enabled: $('schedule-enabled').checked, schedule_mode: $('schedule-mode').value,
      schedule_at: $('schedule-at').value, delay_minutes: $('schedule-delay-minutes').value }));
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
    recipientDetail(); renderContacts(); renderCampaignContacts();
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
    if (kind === 'scheduled' && schedulesLoading) return;
    if (kind === 'scheduled') schedulesLoading = true;
    try {
    if (!more) offsets[kind] = 0;
    const records = await api(kind + '?offset=' + offsets[kind]);
    const list = $(kind === 'events' ? 'event-list' : kind + '-list');
    const expanded = new Set(Array.from(list.querySelectorAll('details[open]')).map(item => item.dataset.id));
    if (!more) list.replaceChildren();
    if (!records.length && !more) list.append(node('p', kind === 'inbox' ? 'No conversation snapshots yet. Read a contact to save one.' : kind === 'scheduled' ? 'No scheduled messages yet. Choose Send later in Compose.' : 'No recorded items yet.', 'viber-empty'));
    for (const item of records) {
      const record = node('details', null, 'viber-record');
      record.dataset.id = item.id || item.lead_id;
      record.open = expanded.has(record.dataset.id);
      if (kind === 'scheduled') {
        record.append(node('summary', `${item.company_name} · ${item.viber_name || 'name verified at dispatch'} · ${item.state}`), node('p', `Send time: ${date(item.scheduled_at)} · Europe/Warsaw`, 'hint'), node('p', item.phone, 'hint'), node('pre', item.text));
        if (item.campaign_name) record.append(node('p', `Campaign: ${item.campaign_name} · ${item.campaign_state}`, 'hint'));
        if (item.state === 'SCHEDULED') {
          if (item.campaign_state === 'PAUSED') record.append(node('p', 'Campaign paused. Review and resume it from Campaigns to continue.', 'hint'));
          else { const countdown = node('p', '', 'send-countdown'); countdown.dataset.due = item.scheduled_at; record.append(countdown); }
          record.append(contactButton('Cancel schedule', async () => {
            await api('cancel-scheduled', {send_id: item.id});
            $('schedule-status').textContent = 'Schedule cancelled. No message will be sent by this schedule.';
            await loadRecords('scheduled');
          }));
        }
        if (item.error) record.append(node('p', item.error, 'warning'));
        if (item.state === 'DISPATCHED') record.append(node('p', 'Send action dispatched; delivery is unverified.', 'hint'));
      } else if (kind === 'sent') {
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
    updateCountdowns();
    } finally { if (kind === 'scheduled') schedulesLoading = false; }
  }

  function updateCountdowns() {
    for (const element of document.querySelectorAll('[data-due]')) {
      const seconds = Math.ceil((new Date(element.dataset.due).getTime() - Date.now()) / 1000);
      element.textContent = seconds > 0 ? `Due in ${Math.ceil(seconds / 60)} minute${seconds > 60 ? 's' : ''}` : 'Due now · waiting for the desktop worker';
    }
  }

  async function loadActivity() {
    const days = await api('activity?days=' + $('activity-days').value);
    for (const state of ['dispatched', 'blocked', 'unknown']) $('activity-' + state).textContent = days.reduce((sum, day) => sum + day[state], 0);
    $('activity-contacts').textContent = contacts.length;
    $('activity-rows').replaceChildren();
    for (const day of days) { const row = node('tr'); row.append(node('th', day.date), node('td', day.dispatched), node('td', day.blocked), node('td', day.unknown)); $('activity-rows').append(row); }
  }

  async function loadDatabaseInbox(more = false) {
    if (databaseLoading) return;
    databaseLoading = true;
    try {
      if (!more) { if (databaseOffset > 50) databaseSignature = null; databaseOffset = 0; }
      const [status, records] = await Promise.all([api('database/status'), api('database/conversations?offset=' + databaseOffset)]);
      const watching = status.state === 'WATCHING';
      $('database-status').className = 'health-card ' + (watching ? 'health-healthy' : 'health-unhealthy');
      $('database-status').replaceChildren(node('strong', watching ? 'Watching for incoming messages' : status.state === 'CONNECTING' ? 'Connecting to Viber’s database…' : 'Message detection paused'),
        node('p', `${status.conversations} conversation${status.conversations === 1 ? '' : 's'} · ${status.messages} saved messages · ${status.new_incoming} incoming text message${status.new_incoming === 1 ? '' : 's'} detected since baseline`),
        node('p', `Last checked: ${date(status.last_poll)} · Automatic replies off`, 'hint'));
      if (status.error) $('database-status').append(node('p', status.error, 'warning'));
      const signature = JSON.stringify(records);
      if (more || signature !== databaseSignature) {
        if (!more) $('database-list').replaceChildren();
        if (!records.length && !more) $('database-list').append(node('p', watching
          ? 'No matching personal conversations yet. Save a contact’s phone number and open a Viber chat with them.'
          : 'Conversation history will appear after the watcher connects.', 'viber-empty'));
        for (const chat of records) {
          const card = node('div', null, 'viber-record'), heading = node('h3', chat.company_name + ' · ' + chat.viber_name);
          card.append(heading, node('p', `${chat.phone} · ${chat.monitoring ? 'Detection on' : 'Detection paused'} · ${chat.new_incoming} new incoming text message${chat.new_incoming === 1 ? '' : 's'}`, 'hint'));
          const actions = node('div', null, 'actions');
          actions.append(contactButton('View history', () => openDatabaseConversation(chat)),
            contactButton(chat.monitoring ? 'Pause detection' : 'Monitor replies', async () => {
              await api('database/monitor', {source_id: chat.source_id, chat_id: chat.chat_id, enabled: !chat.monitoring});
              databaseSignature = null; await loadDatabaseInbox();
            }));
          card.append(actions); $('database-list').append(card);
          if (databaseChat && chat.source_id === databaseChat.source_id && chat.chat_id === databaseChat.chat_id && chat.revision !== databaseRevision) $('database-updated').hidden = false;
        }
        if (!more) databaseSignature = signature;
      }
      databaseOffset += records.length;
      $('database-more').hidden = records.length < 50;
    } finally { databaseLoading = false; }
  }

  async function openDatabaseConversation(chat, older = false) {
    const query = new URLSearchParams({source_id: chat.source_id, chat_id: String(chat.chat_id)});
    if (older && databaseOlder != null) query.set('before', String(databaseOlder));
    const result = await api('database/conversation?' + query);
    databaseChat = chat; databaseOlder = result.older_before; databaseRevision = result.conversation.revision;
    $('database-conversation').hidden = false; $('database-updated').hidden = true;
    $('database-title').textContent = chat.company_name + ' · ' + chat.viber_name;
    $('database-meta').textContent = chat.phone + ' · Retained Viber Desktop history · Automatic replies off';
    $('database-compose').hidden = !chat.leads?.length;
    $('database-older').hidden = databaseOlder == null;
    if (!older) $('database-messages').replaceChildren();
    const fragment = document.createDocumentFragment();
    if (!result.messages.length && !older) fragment.append(node('p', 'No retained messages in this conversation.', 'viber-empty'));
    const types = {2: 'Image', 3: 'Video', 4: 'Sticker', 5: 'Location', 9: 'Link', 10: 'Contact card', 11: 'File or audio', 15: 'System message'};
    for (const message of result.messages) {
      const card = node('article', null, 'database-message ' + (message.direction === 'OUTGOING' ? 'database-outgoing' : 'database-incoming'));
      const label = message.direction === 'OUTGOING' ? 'You · Outgoing' : message.direction === 'INCOMING' ? chat.viber_name + ' · Incoming' + (message.sender_verified ? '' : ' · Sender needs review') : 'Direction needs review';
      card.append(node('strong', label), node('p', date(message.timestamp_ms), 'hint'), node('pre', message.body || `[${types[message.message_type] || 'Unsupported message'}]`));
      const note = message.detection === 'NEW_INCOMING' ? 'New incoming text · detected once' : message.baseline ? 'Imported history · no reply triggered' : message.detection === 'OUTGOING' ? 'Your message · no reply triggered' : message.detection === 'EDITED' ? 'Edited message · no new reply triggered' : 'Stored for review · no reply triggered';
      card.append(node('small', note)); fragment.append(card);
    }
    if (older) $('database-messages').prepend(fragment); else $('database-messages').append(fragment);
  }

  async function navigate(next) {
    view = next;
    for (const id of views) $(id).hidden = id !== next;
    document.querySelectorAll('[data-view]').forEach(button => { if (button.dataset.view === next) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    history.replaceState(null, '', '/viber#' + next);
    if (next === 'contacts') renderContacts();
    if (next === 'campaigns') { renderCampaignContacts(); await loadCampaigns(); }
    if (next === 'activity') await loadActivity();
    if (['events', 'sent', 'inbox', 'scheduled'].includes(next)) await loadRecords(next);
    if (next === 'inbox') await loadDatabaseInbox();
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
        const result = await waitOperation(pending.operation_id);
        $('send-result').textContent = result.state === 'SCHEDULED'
          ? `Message scheduled for ${date(result.scheduled_at)} · Europe/Warsaw. Review or cancel it in Scheduled.`
          : 'Send action dispatched to Viber. Delivery is unverified. Open Sent for the attempt details.';
      } catch (error) {
        $('send-result').textContent = 'The attempt stopped. Review Sent and Viber before trying again.';
        throw error;
      } finally {
        // A known terminal operation can be cleared; a network failure stays saved.
        const state = await api('operations/' + encodeURIComponent(pending.operation_id));
        if (['SUCCEEDED', 'FAILED', 'INTERRUPTED'].includes(state.state)) { pending = null; localStorage.removeItem(pendingKey); preview = null; }
      }
    } catch (error) {
      if (error.status === 400 && pending && !pending.operation_id) {
        pending = null; preview = null; localStorage.removeItem(pendingKey);
      }
      throw error;
    } finally { setBusy(false); }
  }

  function uniqueCampaignContacts() {
    const seen = new Set();
    return contacts.filter(lead => { if (seen.has(lead.phone)) return false; seen.add(lead.phone); return true; });
  }

  function matchingCampaignContacts() {
    const search = $('campaign-search').value.trim().toLocaleLowerCase();
    return uniqueCampaignContacts().filter(lead => [lead.company_name, lead.phone, lead.viber_name || '', lead.id].join(' ').toLocaleLowerCase().includes(search));
  }

  function updateCampaignControls() {
    const locked = busy || campaignWorking || !!campaignPending;
    $('campaign-fields').disabled = locked;
    $('campaign-recover').hidden = !campaignPending;
    $('campaign-recover').disabled = busy || campaignWorking;
    $('campaign-review').hidden = !campaignPreview;
    const expired = campaignPreview && (campaignPreview.expires_at * 1000 <= Date.now() || new Date(campaignPreview.recipients[0].scheduled_at).getTime() <= Date.now());
    $('campaign-confirm-check').disabled = locked || !!expired;
    $('campaign-close-review').disabled = locked;
    $('campaign-activate').disabled = locked || !campaignPreview || !!expired || !$('campaign-confirm-check').checked;
    $('campaign-activate').textContent = campaignPreview ? `Schedule ${campaignPreview.recipients.length} messages` : 'Schedule campaign';
    $('campaign-review-expiry').textContent = expired ? 'This review expired or its first send time passed. Close it and review the campaign again.' : 'This review expires after ten minutes, or when the first send time passes.';
    const available = uniqueCampaignContacts().length;
    $('campaign-selection-count').textContent = `${campaignSelection.size} selected · ${available} unique phone numbers available · maximum 100. Duplicate phone numbers receive one message.`;
    $('campaign-select').textContent = `Select first ${$('campaign-batch-size').value || 100}`;
    $('campaign-create').disabled = locked || campaignSelection.size === 0;
    for (const button of $('campaign-list').querySelectorAll('button')) button.disabled = locked;
  }

  function renderCampaignContacts() {
    const valid = new Set(uniqueCampaignContacts().map(lead => lead.id));
    for (const id of campaignSelection) if (!valid.has(id)) campaignSelection.delete(id);
    const list = $('campaign-contact-list'); list.replaceChildren();
    const matches = matchingCampaignContacts();
    if (!matches.length) list.append(node('p', 'No matching contacts. Add numbers in Contacts first.', 'viber-empty'));
    for (const lead of matches) {
      const label = node('label'), check = node('input'), text = node('span', lead.company_name + (lead.viber_name ? ' · ' + lead.viber_name : ''));
      check.type = 'checkbox'; check.checked = campaignSelection.has(lead.id);
      check.disabled = busy || campaignWorking || !!campaignPending;
      text.append(node('small', `${lead.phone} · #${lead.id}`)); label.append(check, text); list.append(label);
      check.addEventListener('change', () => {
        if (check.checked && campaignSelection.size >= 100) { check.checked = false; showError(new Error('Select at most 100 unique numbers per campaign.')); return; }
        if (check.checked) campaignSelection.add(lead.id); else campaignSelection.delete(lead.id);
        updateCampaignControls();
      });
    }
    updateCampaignControls();
  }

  function campaignRecipientRow(item, index, reviewing = false) {
    const lead = reviewing ? item.lead : item;
    const row = node('details', null, 'viber-record');
    row.append(node('summary', `${index + 1}. ${lead.company_name} · ${lead.phone}${reviewing ? '' : ' · ' + item.state}`),
      node('p', `Viber name: ${item.viber_name || 'verify and discover at send time'} · ${date(item.scheduled_at)} · Europe/Warsaw`, 'hint'), node('pre', item.text));
    if (item.error) row.append(node('p', item.error, 'warning'));
    return row;
  }

  async function reviewCampaign(id) {
    if (campaignPending) throw new Error('Check the saved campaign submission first.');
    campaignPreview = await api('campaign-review', {campaign_id: id});
    $('campaign-review-title').textContent = 'Review: ' + campaignPreview.name;
    const items = campaignPreview.recipients, rules = campaignPreview.rules;
    $('campaign-review-meta').textContent = `${items.length} remaining recipients · ${rules.interval_minutes} minutes apart · up to ${rules.daily_cap}/day · ${rules.window_start}–${rules.window_end} Europe/Warsaw. First: ${date(items[0].scheduled_at)}. Last: ${date(items.at(-1).scheduled_at)}.`;
    $('campaign-review-recipients').replaceChildren(...items.map((item, index) => campaignRecipientRow(item, index, true)));
    $('campaign-confirm-check').checked = false; updateCampaignControls();
    $('campaign-review').scrollIntoView({block: 'start', behavior: 'smooth'});
  }

  async function loadCampaigns() {
    if (campaignsLoading) return;
    campaignsLoading = true;
    try {
      const campaigns = await api('campaigns'), list = $('campaign-list');
      const signature = JSON.stringify(campaigns);
      if (signature === campaignsSignature) { updateCampaignControls(); return; }
      const expanded = new Set(Array.from(list.querySelectorAll('details[open]')).map(item => item.dataset.id));
      const cards = [];
      for (const item of campaigns) {
        const card = node('article', null, 'campaign-card');
        card.append(node('h3', item.name), node('span', item.state, 'viber-state'),
          node('p', `${item.total} recipients · ${Object.entries(item.counts).map(([state, count]) => `${count} ${state.toLowerCase()}`).join(' · ')}`),
          node('p', `${item.rules.interval_minutes} minutes apart · up to ${item.rules.daily_cap}/day · ${item.rules.window_start}–${item.rules.window_end} Europe/Warsaw`, 'hint'));
        if (item.next_at) card.append(node('p', `Next planned: ${date(item.next_at)} · Last planned: ${date(item.last_at)}`, 'hint'));
        if (item.reason) card.append(node('p', item.reason, 'warning'));
        if (item.duplicates_skipped) card.append(node('p', `${item.duplicates_skipped} duplicate phone selection(s) skipped.`, 'hint'));
        const actions = node('div', null, 'actions');
        if (['DRAFT', 'PAUSED'].includes(item.state) && ((item.counts.DRAFT || 0) + (item.counts.SCHEDULED || 0))) actions.append(contactButton(item.state === 'DRAFT' ? 'Review and schedule' : 'Review and resume', () => reviewCampaign(item.id)));
        if (item.state === 'ACTIVE') actions.append(contactButton('Pause campaign', async () => {
          await api('campaign-pause', {campaign_id: item.id}); $('campaign-status').textContent = 'Campaign paused. An action already sending may finish.'; await loadCampaigns();
        }));
        if (['DRAFT', 'PAUSED', 'ACTIVE'].includes(item.state)) actions.append(contactButton('Cancel campaign', async () => {
          await api('campaign-cancel', {campaign_id: item.id});
          if (campaignPreview?.campaign_id === item.id) campaignPreview = null;
          $('campaign-status').textContent = 'Campaign cancelled. Remaining messages will not start; an action already sending may finish.'; updateCampaignControls(); await loadCampaigns();
        }));
        card.append(actions);
        const details = node('details'), body = node('div'); details.dataset.id = item.id;
        details.append(node('summary', 'Recipients and outcomes'), body); card.append(details);
        const fillDetails = async () => {
          const detail = await api('campaigns/' + encodeURIComponent(item.id));
          body.replaceChildren(...detail.recipients.map((recipient, index) => campaignRecipientRow(recipient, index)));
        };
        if (expanded.has(item.id)) { details.open = true; await fillDetails(); }
        details.addEventListener('toggle', () => { if (details.open && !body.childElementCount) fillDetails().catch(showError); });
        cards.push(card);
      }
      list.replaceChildren(...(cards.length ? cards : [node('p', 'No campaigns yet. Create a draft above to get started.', 'viber-empty')]));
      campaignsSignature = signature;
      updateCampaignControls();
    } finally { campaignsLoading = false; }
  }

  async function recoverCampaignSubmission() {
    if (!campaignPending || campaignWorking) return;
    campaignWorking = true; updateCampaignControls();
    let result, kind;
    try {
      kind = campaignPending.kind;
      result = await api(kind === 'create' ? 'campaigns' : 'campaign-activate', campaignPending.payload);
      campaignPending = null; localStorage.removeItem(campaignPendingKey);
    } catch (error) {
      if (error.status === 400) { campaignPending = null; localStorage.removeItem(campaignPendingKey); }
      throw error;
    } finally { campaignWorking = false; updateCampaignControls(); }
    if (kind === 'create') {
      $('campaign-status').textContent = `Draft saved: ${result.total} unique recipients. Review and confirm to activate it.`;
      $('campaign-create-panel').open = false; await loadCampaigns(); await reviewCampaign(result.id);
    } else {
      campaignPreview = null; $('campaign-confirm-check').checked = false; updateCampaignControls();
      $('campaign-status').textContent = `${result.name}: ${result.state}. ${result.counts.SCHEDULED || 0} messages waiting. Next planned: ${date(result.next_at)}. Keep the local server running.`;
      await loadCampaigns();
    }
  }

  $('campaign-start').value = localDateTime(new Date(Date.now() + 3600000));
  $('campaign-search').addEventListener('input', renderCampaignContacts);
  $('campaign-batch-size').addEventListener('input', updateCampaignControls);
  $('campaign-select').addEventListener('click', action(async () => {
    const count = Number($('campaign-batch-size').value);
    if (!Number.isInteger(count) || count < 1 || count > 100) throw new Error('Choose a batch size between 1 and 100.');
    campaignSelection.clear(); for (const lead of matchingCampaignContacts().slice(0, count)) campaignSelection.add(lead.id);
    renderCampaignContacts();
  }));
  $('campaign-clear').addEventListener('click', () => { campaignSelection.clear(); renderCampaignContacts(); });
  $('campaign-form').addEventListener('submit', action(async () => {
    if (campaignPending) throw new Error('Check the saved campaign submission first.');
    if (!campaignSelection.size) throw new Error('Choose at least one recipient.');
    campaignPending = {kind: 'create', payload: {request_key: crypto.randomUUID(), name: $('campaign-name').value, text: $('campaign-text').value,
      lead_ids: [...campaignSelection], schedule: {mode: 'datetime', local_time: $('campaign-start').value, time_zone: timeZone},
      interval_minutes: Number($('campaign-interval').value), daily_cap: Number($('campaign-cap').value), window_start: $('campaign-window-start').value, window_end: $('campaign-window-end').value}};
    localStorage.setItem(campaignPendingKey, JSON.stringify(campaignPending)); await recoverCampaignSubmission();
  }));
  $('campaign-confirm-check').addEventListener('change', updateCampaignControls);
  $('campaign-close-review').addEventListener('click', () => { campaignPreview = null; updateCampaignControls(); });
  $('campaign-activate').addEventListener('click', action(async () => {
    if (!campaignPreview || !$('campaign-confirm-check').checked || $('campaign-activate').disabled) throw new Error('Review and confirm this campaign first.');
    campaignPending = {kind: 'activate', payload: {preview_token: campaignPreview.token, confirmed: true, request_key: crypto.randomUUID()}};
    localStorage.setItem(campaignPendingKey, JSON.stringify(campaignPending)); await recoverCampaignSubmission();
  }));
  $('campaign-recover').addEventListener('click', action(recoverCampaignSubmission));

  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', action(() => navigate(button.dataset.view))));
  $('to').addEventListener('change', () => { preview = null; recipientDetail(); saveDraft(); updateCompose(); });
  $('body').addEventListener('input', () => { preview = null; saveDraft(); updateCompose(); });
  for (const id of ['schedule-enabled', 'schedule-mode', 'schedule-at', 'schedule-delay-minutes']) $(id).addEventListener('change', () => {
    preview = null;
    if ($('schedule-enabled').checked && !$('schedule-at').value) $('schedule-at').value = localDateTime(new Date(Date.now() + 3600000));
    updateCompose(); saveDraft();
  });
  $('compose-form').addEventListener('submit', action(async () => {
    if (pending) throw new Error('Check the saved send attempt first.');
    const schedule = schedulePayload();
    const reviewed = await operation('prepare', { lead_id: Number($('to').value), text: $('body').value, schedule }, 'Opening the dial pad and verifying this recipient. No message is sent during review…');
    if (schedule && !reviewed.scheduled_at) throw new Error('The server needs to restart to enable scheduling. Refresh after the update.');
    preview = reviewed;
    $('confirm-recipient').replaceChildren();
    for (const [label, value] of [['Business', preview.lead.company_name], ['Viber name', preview.viber_name], ['Phone', preview.lead.phone], ['Android contact', preview.lead.contact_name]]) $('confirm-recipient').append(node('dt', label), node('dd', value));
    if (preview.scheduled_at) $('confirm-recipient').append(node('dt', 'Send time'), node('dd', date(preview.scheduled_at) + ' · Europe/Warsaw'));
    $('confirm-label').textContent = preview.scheduled_at ? 'I checked this recipient, message, and scheduled time.' : 'I checked this recipient and message.';
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
  $('new-message').addEventListener('click', () => { preview = null; $('body').value = ''; $('schedule-enabled').checked = false; $('send-result').textContent = ''; saveDraft(); updateCompose(); });
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
  for (const kind of ['sent', 'events', 'inbox', 'scheduled']) $(kind === 'events' ? 'event-more' : kind + '-more').addEventListener('click', action(() => loadRecords(kind, true)));
  $('refresh').addEventListener('click', action(async () => { await loadContacts(); if (view === 'health') await checkHealth(); else await navigate(view); }));
  $('database-refresh').addEventListener('click', action(() => loadDatabaseInbox()));
  $('database-more').addEventListener('click', action(() => loadDatabaseInbox(true)));
  $('database-older').addEventListener('click', action(() => openDatabaseConversation(databaseChat, true)));
  $('database-history-refresh').addEventListener('click', action(() => openDatabaseConversation(databaseChat)));
  $('database-close').addEventListener('click', () => { $('database-conversation').hidden = true; databaseChat = null; });
  $('database-compose').addEventListener('click', action(async () => { selectLead(databaseChat.leads[0].id); await navigate('compose'); }));
  window.addEventListener('storage', event => {
    if (event.key === pendingKey) { try { pending = JSON.parse(event.newValue || 'null'); updateCompose(); } catch (error) { showError(error); } }
    if (event.key === campaignPendingKey) { try { campaignPending = JSON.parse(event.newValue || 'null'); updateCampaignControls(); } catch (error) { showError(error); } }
  });
  setInterval(() => { if (preview) updateCompose(); if (campaignPreview) updateCampaignControls(); }, 1000);
  setInterval(() => {
    updateCountdowns();
    if (csrf && !document.hidden && view === 'scheduled' && !busy && offsets.scheduled <= 100) loadRecords('scheduled').catch(showError);
    if (csrf && !document.hidden && view === 'campaigns' && !busy && !campaignWorking && !campaignPending) loadCampaigns().catch(showError);
    if (csrf && !document.hidden && view === 'inbox' && !busy && databaseOffset <= 50) loadDatabaseInbox().catch(showError);
  }, 5000);
  action(async () => {
    const session = await api('session', {}); csrf = session.csrf; await loadContacts();
    try { const draft = JSON.parse(localStorage.getItem(draftKey) || 'null'); if (draft) {
      $('to').value = draft.lead_id || ''; $('body').value = draft.text || '';
      $('schedule-enabled').checked = !!draft.schedule_enabled; $('schedule-mode').value = draft.schedule_mode === 'delay' ? 'delay' : 'datetime';
      $('schedule-at').value = draft.schedule_at || ''; $('schedule-delay-minutes').value = draft.delay_minutes || '60'; recipientDetail();
    } } catch { /* A broken unsent draft can be rewritten. */ }
    updateCompose();
    const requested = location.hash.slice(1); await navigate(views.includes(requested) ? requested : 'compose');
    if (pending) $('send-result').textContent = 'A send attempt is saved. Check its status before composing another message.';
    if (campaignPending) $('campaign-status').textContent = 'A campaign submission is saved. Check its status before creating or activating another.';
  })();
})();
