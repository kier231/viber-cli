"""CSV leads are saved without desktop actions; dispatch verifies every number."""
from contextlib import closing
import csv
import io
import unicodedata

from app.models import validate_company_name
from app.phone import normalize_serbian_phone
from app.viber_inbox import utc_now


def header(value):
    value = unicodedata.normalize('NFKD', value.strip().lower().lstrip('\ufeff'))
    return '_'.join(''.join(c for c in value if not unicodedata.combining(c)).split()).replace('-', '_')


ALIASES = {
    'phone': ('phone', 'public_mobile', 'phone_number', 'telefon', 'broj_telefona', 'mobile'),
    'company': ('company_name', 'business', 'business_name', 'company', 'naziv', 'firma'),
    'description': ('opis_klijenta', 'client_description', 'codex_agent_note_sr', 'description'),
    'text': ('sales_pitch_sr', 'outreach_message', 'message', 'poruka', 'pitch'),
}


def parse_csv(text):
    from app.web_service import validate_message
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Choose a UTF-8 CSV file with a header and at least one lead.')
    if len(text.encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('CSV files must be smaller than 8 MB.')
    text = text.lstrip('\ufeff')
    try:
        # Detect from the header, so commas in a quoted message cannot change it.
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=',;\t')
        reader = csv.reader(io.StringIO(text, newline=''), dialect, strict=True)
        fields = [header(value) for value in next(reader)]
        if len(set(fields)) != len(fields) or not all(fields):
            raise ValueError('CSV headers must be unique and nonempty.')
        mapping = {}
        for key, aliases in ALIASES.items():
            matches = [fields.index(alias) for alias in aliases if alias in fields]
            if len(matches) > 1:
                raise ValueError(f'Use only one CSV column for {key}.')
            mapping[key] = matches[0] if matches else None
        if mapping['phone'] is None or mapping['company'] is None:
            raise ValueError('CSV needs phone and company_name columns (public_mobile and business also work).')
        leads = []
        for values in reader:
            if not values or not any(value.strip() for value in values):
                continue
            try:
                if len(values) != len(fields):
                    raise ValueError('Column count does not match the header.')
                item = {key: values[index].strip() if index is not None else '' for key, index in mapping.items()}
                item['phone'] = normalize_serbian_phone(item['phone'])
                item['company'] = validate_company_name(item['company'])
                description = item['description']
                if len(description) > 4000 or any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in description):
                    raise ValueError('Opis klijenta must contain at most 4,000 characters without invalid controls.')
                if item['text']:
                    item['text'] = validate_message(item['text'])
                leads.append(item)
            except ValueError as exc:
                raise ValueError(f'CSV line {reader.line_num}: {exc} Nothing was imported.') from exc
    except (csv.Error, StopIteration) as exc:
        raise ValueError(f'Cannot read CSV: {exc}. Nothing was imported.') from exc
    if not leads:
        raise ValueError('The CSV contains no leads.')
    return leads


class CsvImport:
    def __init__(self, service):
        self.service = service
        with closing(service.store._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS contact_outreach (
                account_id TEXT NOT NULL, lead_id INTEGER NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY(account_id,lead_id))''')

    def import_leads(self, text):
        rows = parse_csv(text)  # Validate the entire file before changing the ledger.
        service = self.service
        result = {'imported': 0, 'duplicates_skipped': 0, 'other_account_skipped': 0, 'lead_ids': []}
        with service.lock, closing(service.store._connect()) as db, db:
            if service.closed:
                raise ValueError('The localhost app is stopping.')
            if service.accounts:
                service.accounts.authenticate(service.accounts.token, service.account_id,
                                              service.accounts.worker_id, service.accounts.epoch)
            db.execute('BEGIN IMMEDIATE')
            seen = set()
            for item in rows:
                phone = item['phone']
                if phone in seen:
                    result['duplicates_skipped'] += 1
                    continue
                seen.add(phone)
                if service.accounts:
                    owner = db.execute('SELECT account_id FROM contact_owners WHERE phone=?', (phone,)).fetchone()
                    if owner and owner[0] != service.account_id:
                        result['other_account_skipped'] += 1
                        continue
                if db.execute('SELECT 1 FROM leads WHERE phone=?', (phone,)).fetchone():
                    result['duplicates_skipped'] += 1
                    continue
                if service.accounts:
                    service.accounts.claim(phone, service.account_id, db)
                    # Incoming ingestion can have created the contact while we waited for ownership.
                    if db.execute('SELECT 1 FROM leads WHERE phone=?', (phone,)).fetchone():
                        result['duplicates_skipped'] += 1
                        continue
                lead = service.store.create_verified(phone, item['company'], None, db)
                if item['description']:
                    db.execute('INSERT INTO contact_descriptions(account_id,lead_id,description,revision,updated_at) VALUES(?,?,?,1,?)',
                               (service.account_id, lead.id, item['description'], utc_now()))
                if item['text']:
                    db.execute('INSERT INTO contact_outreach VALUES(?,?,?)', (service.account_id, lead.id, item['text']))
                result['lead_ids'].append(lead.id)
                result['imported'] += 1
            db.execute('INSERT INTO web_events(created_at,kind,detail,account_id) VALUES(?,?,?,?)',
                       (utc_now(), 'CSV_IMPORTED', f"Imported {result['imported']} leads; skipped {result['duplicates_skipped']} duplicates and {result['other_account_skipped']} other-account numbers.", service.account_id))
        return result
