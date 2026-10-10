"""Fixed, read-only Viber queries and conservative direct-chat identification."""

from collections import defaultdict
import hashlib
from pathlib import Path
import re


class DatabaseReadError(RuntimeError):
    pass


TEXT_MESSAGE_TYPES = (1, 9)  # Plain text and text with a Viber link preview.


def international_phone(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r'[\s().-]', '', value)
    if value.startswith('00'):
        value = value[2:]
    if value.startswith('+'):
        value = value[1:]
    return '+' + value if re.fullmatch(r'[1-9][0-9]{7,14}', value) else None


def source_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    created = getattr(stat, 'st_birthtime_ns', 0)
    return hashlib.sha256(f'{str(path).lower()}|{stat.st_ino}|{created}'.encode()).hexdigest()


REQUIRED = {
    'Events': {'EventID', 'TimeStamp', 'Direction', 'ChatID', 'ContactID', 'Token', 'SortOrder'},
    'Messages': {'EventID', 'Type', 'Body', 'ClientFlag'},
    'Contact': {'ContactID', 'Name', 'ClientName', 'Number'},
    'ChatInfo': {'ChatID', 'Name', 'Flags', 'PGType', 'PGUri'},
    'ChatRelation': {'ChatID', 'ContactID'},
}


def validate_schema(query):
    for table, required in REQUIRED.items():
        columns = {row['name'] for row in query(f'PRAGMA table_info({table})')}
        if not required <= columns:
            raise DatabaseReadError('Viber database schema changed; message detection is paused.')


def read_snapshot(query, *, source_id, own_phone, phones, after_event_id=0, include_new_senders=False, incoming_since_ms=0):
    """Caller holds a read transaction. Reconciles all retained contact history.

    IDs, directions and sender membership come from Viber, never message text.
    Only saved contact numbers and unambiguously identified direct chats leave
    the reader process. New chats always receive their own history baseline.
    """
    own_phone = international_phone(own_phone)
    if not own_phone:
        raise DatabaseReadError('Cannot identify the local Viber account phone number.')
    phones = {international_phone(phone) for phone in phones} - {None, own_phone}
    contacts = {int(row['ContactID']): row for row in query(
        'SELECT ContactID,Name,ClientName,Number FROM Contact')}
    self_ids = {key for key, row in contacts.items() if international_phone(row['Number']) == own_phone}
    if not self_ids:
        raise DatabaseReadError('The Viber database does not identify the local account. Detection is paused.')
    relations = defaultdict(set)
    for row in query('SELECT ChatID,ContactID FROM ChatRelation'):
        relations[int(row['ChatID'])].add(int(row['ContactID']))
    chats = []
    for row in query('SELECT ChatID,Name,Flags,PGType,PGUri FROM ChatInfo'):
        chat_id = int(row['ChatID'])
        peers = relations[chat_id] - self_ids
        # Named chats, public chats, self-chats and ambiguous membership are
        # deliberately excluded from personal-contact reply detection.
        # This Windows build uses PGType=255 and Flags=0 for personal chats.
        # Unknown flag combinations remain excluded until explicitly verified.
        if (row['Name'] or row['PGUri'] or row['Flags'] != 0
                or row['PGType'] not in (None, 0, 255) or len(peers) != 1):
            continue
        peer_id = next(iter(peers))
        peer = contacts.get(peer_id)
        phone = international_phone(peer['Number']) if peer else None
        if phone not in phones:
            if not include_new_senders or not phone or phone==own_phone:
                continue
            fresh = query('''SELECT e.EventID FROM Events e JOIN Messages m ON m.EventID=e.EventID
                WHERE e.ChatID=? AND e.ContactID=? AND e.Direction=0 AND e.EventID>?
                AND e.TimeStamp>=? AND m.Type IN (1,9)
                AND COALESCE(m.ClientFlag,0) NOT IN (256,257)
                AND LENGTH(TRIM(COALESCE(m.Body,'')))>0 LIMIT 1''',(chat_id,peer_id,after_event_id,incoming_since_ms))
            if not fresh:
                continue
        chats.append({'chat_id': chat_id, 'peer_id': peer_id, 'phone': phone,
                      'viber_name': peer['ClientName'] or peer['Name'] or phone})
    max_id = query('SELECT COALESCE(MAX(EventID),0) AS maximum FROM Events')[0]['maximum']
    if int(max_id) < after_event_id:
        raise DatabaseReadError('Viber history was reset or replaced. Detection is paused; rebaseline required.')
    messages = []
    for chat in chats:
        # Re-read retained history on database changes, including old edits and
        # deletions. The worker skips the scan when SQLite data_version agrees.
        rows = query('''SELECT e.EventID AS event_id,e.ChatID AS chat_id,
            e.ContactID AS sender_id,e.Direction AS direction,e.TimeStamp AS timestamp_ms,
            e.Token AS token,e.SortOrder AS sort_order,m.Type AS message_type,
            m.Body AS body,m.ClientFlag AS client_flag
            FROM Events e JOIN Messages m ON m.EventID=e.EventID
            WHERE e.ChatID=?
            ORDER BY e.TimeStamp,e.SortOrder,e.EventID''',
            (chat['chat_id'],))
        for row in rows:
            row['token'] = str(row['token']) if row['token'] is not None else None
            row['direction'] = {0: 'INCOMING', 1: 'OUTGOING'}.get(row['direction'], 'UNKNOWN')
            row['body'] = row['body'] or ''
            row['sender_verified'] = row['direction'] == 'OUTGOING' or row['sender_id'] == chat['peer_id']
            messages.append(row)
    return {'source_id': source_id, 'account_phone': own_phone, 'chats': chats,
            'messages': messages, 'max_event_id': int(max_id), 'incoming_since_ms':incoming_since_ms}
