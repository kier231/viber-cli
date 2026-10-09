# viber-cli

A manual Windows console tool for keeping an Android contact ledger and opening,
sending to, or reading Viber Desktop chats. The default chat path enters a phone
number on Viber's dial pad and presses its message button without moving the
Windows mouse or taking keyboard focus. It also includes a localhost web app
that preserves the existing EmailOutreach UI and adds a ViberOutreach workspace.
Manual, scheduled, and campaign Viber actions run one at a time.

## Localhost app

```powershell
cd C:\viber-cli
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe web.py
```

Open **http://127.0.0.1:4001/**. Click the **EmailOutreach** title to open the
dropdown and select **ViberOutreach**. The title in either workspace switches
back to the other. `start-web.ps1` is an alternative launcher; use
`-Port 4002` to select another free port.

The email workspace forwards to your existing EmailOutreach service on port
4000. Keep that Docker service running. Its original UI, mailboxes, contacts,
CSV imports, campaigns, scheduling, activity, health, deliverability, inbox
placement, events, sent mail, replies, and inbox remain available. Its styling
and JavaScript are served by the original service; the only page change is the
title dropdown. Email browser sessions use a separate cookie so the app on
port 4000 remains usable. No SMTP/IMAP credentials are copied into this repo.
Use `--email-port` if the existing service uses another loopback port.

ViberOutreach provides compose with recipient review, contact search and
pagination, verified Android contact creation, separate Viber name editing,
background open/read/send, scheduled messages with cancellation, campaigns of
up to 100 unique recipients, current-chat
reading, saved visible conversation snapshots, send history, daily activity, an event log, and setup/control
diagnostics. It shares `leads.sqlite3` with the CLI and uses the same business
and Android contact names. Sending opens the phone through the dial pad,
reads the Viber name, shows the exact recipient and text, then requires a
checkbox and **Send message**. It verifies the recipient again at dispatch.

### Scheduling Viber messages

In **ViberOutreach → Compose**, select a contact, write the message, and check
**Send later**. Choose a date and time or **Send in X minutes**. Press
**Review scheduled message**, check the recipient, text, and exact time, then
confirm with **Schedule message**. A delay is measured from the review request;
the confirmation displays the resulting fixed send time. Dates use
**Europe/Warsaw**, including daylight saving, independently of the Windows time
zone. Repeated or nonexistent daylight-saving times must be changed, or you
can use a delay instead. Send times can be up to 365 days ahead.

The **Scheduled** page shows saved messages, countdowns, outcomes, and a
**Cancel schedule** button for messages that have not started. Queued messages
are saved in SQLite and survive app restarts. The scheduler runs in the local
server, so the browser tab can be closed; keep `web.py` running, the PC awake,
Windows unlocked, and Viber restored behind another window. The scheduler
uses the same desktop worker as manual actions and rechecks the saved contact,
phone number, Viber name, and message before dispatch.

After a restart or a busy desktop worker, a message up to 15 minutes overdue
can still run. Older messages become **MISSED** and are not sent. A failed
recipient/setup check becomes **BLOCKED**; an uncertain attempt becomes
**UNKNOWN**. Neither is retried automatically. Review Viber and create a new
schedule if needed. Cancellation fails once the send operation has started.

### Viber campaigns

In **ViberOutreach → Campaigns**, open **Create a campaign**. Enter a name,
select contacts individually or use **Select first 100**, and write the message
template. Choose the start time, minutes between messages, maximum messages
per day, and daily sending window. All times use **Europe/Warsaw**. Messages
outside the window or above the daily cap continue on the next day. A campaign
must finish within 365 days. Each campaign accepts 1–100 contacts and sends
once per unique phone number, even when the ledger has duplicate rows.

Supported template variables are `{{company}}`, `{{phone}}`, `{{name}}`
(saved Viber name, otherwise business name), and `{{viber_name}}` (requires a
saved Viber name). Expanded text is fixed in the draft and shown individually
for every recipient. Messages use the same single-line plain text rules as
Compose. **Create draft** saves the batch without sending anything. Review
every recipient, expanded message, and planned time, check the authorization
box, then press **Schedule N messages**. Campaign reviews expire after ten
minutes or when the first planned send time passes.

The scheduler opens each phone through the desktop dial pad and verifies its
Viber name immediately before dispatch. Previously unknown Viber names are
discovered and saved separately; business and Android contact names stay as
they are. A known name mismatch or changed recipient blocks the attempt.
Sending intervals and daily caps are enforced against actual attempts, so a
busy worker cannot cause a burst of overdue messages. Remaining planned times
adjust after each attempt to preserve that spacing and the daily window.

Use **Pause campaign**, **Review and resume**, or **Cancel campaign** to manage
the batch. Pause and cancel stop remaining messages; an action already sending
may finish. A blocked, missed (over 15 minutes overdue), or uncertain attempt
automatically pauses the campaign. Inspect Viber before reviewing and resuming
the remaining recipients. Failed or uncertain messages are never retried by
Resume. Interrupted attempts become unknown on restart and pause the batch.
The campaign list shows recipient counts, status, planned times, and individual
outcomes. Dispatched counts mean send actions, not delivery receipts.

Drafts and active campaigns survive restarts. Keep the local server running,
the PC awake, Windows unlocked, and Viber restored behind another window;
the browser can be closed. Email campaigns remain in EmailOutreach.

Viber's visible messages are not a full inbox, and accessibility does not
always reveal direction or delivery receipts. The UI labels these as snapshots
and send actions. Email scheduling and campaigns stay in EmailOutreach.
Reviewing a message, reading chats, switching workspaces, and checking health
do not send it. Confirmed Viber schedules dispatch automatically when due
while the server is running, including eligible overdue schedules at restart.

### Background incoming-message detection

The server now starts a separate, read-only Viber database watcher. It uses
Viber's installed Windows Qt SQLite driver and discovers candidate database
keys through read-only access to your running Viber process. Keys are validated
against your database and remain in memory: they are never logged, saved, or
passed on the command line. The reader does not activate, restore, pause, or
type into Viber and does not need its window visible. Viber must be running
and logged in. Sending still has the desktop-window requirements above.

Install the updated `requirements.txt` before starting the server. The tested
Windows Viber build uses the Qt 6.11 ABI series; `PySide6-Essentials==6.11.2`
provides the isolated SQL bindings. A Qt version mismatch, unreadable key,
changed schema, reused message identity, or reset history pauses detection
with a visible error. The reader retries after 30 seconds. If key discovery
fails, restart Viber and retry. For multiple profiles, set `VIBER_CLI_PROFILE`
to the chosen profile directory containing `viber.db`; `VIBER_CLI_VIBER_DIR`
can select a nondefault Viber installation. Do not delete the app's saved
message ledger to resolve a source error.

Only personal conversations matched to saved contact phone numbers are
imported. Named/group/public/self chats and unsupported chat flags are excluded.
Business and Android names are preserved; Viber's profile name is displayed
separately. The first import for each conversation is a history baseline and
never counts as a new reply. Later messages use Viber's native event ID, chat
ID, direction, sender membership, and timestamp. Identical message text is not
used for deduplication. Outgoing messages, edits, reactions, system events, and
older-history backfills cannot become new incoming text triggers. Unknown
directions or unsupported message types are retained for review.

SQLite source reads run in short read-only transactions, including committed
WAL changes. Full retained history for matched contacts is reconciled whenever
the database changes, including old edits and deletions. The message ledger
and checkpoints commit together in `leads.sqlite3`; restarts resume detection
without reimporting messages as new. Conversation revisions change on new,
edited, deleted, or manually sent messages, providing a context check for a
future reply worker. This release only records messages: **no Codex calls or
automatic replies are enabled**. Existing authorized campaigns and schedules
continue to work independently.

The added local tables are `viber_sources`, `viber_conversations`, and
`viber_messages`. They contain private conversation history and are covered by
the existing database Git exclusions. No Viber source database is modified.

The server binds only to `127.0.0.1` and checks the Host, Origin, and browser
tokens. Viber desktop actions run on one COM worker so web requests remain
responsive. Reviewed messages expire after two minutes. Submission keys are
saved before dispatch to prevent duplicate attempts after a lost response.
An interrupted/uncertain attempt is recorded as unknown and is never retried
automatically; check Viber manually. A second server cannot use the same
database while the first is running. Stop the app with Ctrl+C; it finishes any
active operation before exiting.

The extra SQLite tables are `web_operations`, `web_sends`, `web_reads`, and
`web_events`, `web_campaigns`, and `web_campaign_requests`. `web_sends` also stores the scheduled UTC time, campaign and attempt information, and the reviewed
recipient snapshot. These tables contain local message text and snapshots and are ignored by
Git with the contact database. The old CLI commands continue to work; avoid
running a separate CLI desktop action during a web desktop action.

## Requirements

- Windows 11 and Python 3.12 or newer.
- Viber Desktop installed, running, logged in, and restored behind your other
  windows (not minimized). The background controls were validated on the Viber
  Qt build installed on this PC; another build may expose different controls.
- Tesseract OCR available on `PATH`, with `eng` and `srp_latn` language data.
  The CLI uses it to read the chat header from a background window capture.
- Android Platform Tools (`adb`), USB debugging authorized, and exactly one
  usable device connected. The CLI checks `PATH`, common Windows install paths
  (including `C:\adb\platform-tools`), and `VIBER_CLI_ADB`.
- An unlocked Windows session. The background mode leaves your foreground app,
  system mouse, keyboard, and clipboard alone. It cannot run in a disconnected
  Windows session.

Check your Python version, create a virtual environment, and install packages:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use any installed Python version of 3.12 or newer. For example, Python 3.13
works; `py -3.12` fails when 3.12 is not installed. The commands above do not
require PowerShell activation. If you prefer activation, use
`.\.venv\Scripts\Activate.ps1` after creating the environment.

Verify the phone and inspect Viber before adding a contact:

```powershell
& 'C:\adb\platform-tools\adb.exe' devices -l
.\.venv\Scripts\python.exe main.py inspect
```

If ADB is installed elsewhere, set its path for this PowerShell session with
`$env:VIBER_CLI_ADB = 'C:\path\to\platform-tools\adb.exe'`. The CLI also
accepts `adb` on `PATH` automatically. `inspect` requires Viber Desktop to be
running and logged in.

`inspect` prints the entire accessible Viber subtree, including control name,
type, and automation ID. It does not send anything. Inspect first on the Viber
version actually installed. Search, header, composer, and message exposure vary
by version. On the tested Viber Desktop build, the recipient header is not
exposed through UI Automation, so background commands read it from a window
capture. If the header cannot be read, sending stops.

## Commands

```powershell
.\.venv\Scripts\python.exe main.py add-contact "+381641234567" "Auto Servis Markovic"
.\.venv\Scripts\python.exe main.py contacts
.\.venv\Scripts\python.exe main.py set-viber-name 1 "Person's Viber name"
.\.venv\Scripts\python.exe main.py open 1
.\.venv\Scripts\python.exe main.py send 1 "Pozdrav, hteo sam nesto da vas pitam."
.\.venv\Scripts\python.exe main.py read 1
.\.venv\Scripts\python.exe main.py chat 1
.\.venv\Scripts\python.exe main.py read-current
.\.venv\Scripts\python.exe main.py --debug inspect
.\.venv\Scripts\python.exe main.py
```

`chat` accepts `/read` and `/exit`. The menu calls the same command functions.
`open`, `send`, `read <id>`, and `chat` use the dial pad in background by default.
Use `--foreground` only to try the older contact-name search, which takes focus
and may fail on this Viber build because its header is not accessible.

Only a literal lowercase `y` at a send prompt authorizes sending. Background
mode reads back each dial-pad digit, presses the message icon, and checks a
stable chat name from a Windows `PrintWindow` capture. `Unknown`, unreadable,
or changed headers block sending. It also checks the draft and chat name again
before posting the Send button click. Background typing supports one line of
plain text with Serbian Latin characters, but not emoji. `SENT` means the draft
cleared after the click; it is not a delivery receipt.

The SQLite database is `leads.sqlite3` next to `main.py`. Set `VIBER_CLI_DB` to
an alternate path. The database stores `id`, `phone`, `company_name`,
`viber_name`, `contact_name`, and `created_at`. It does not contain messages.
Existing databases gain the nullable `viber_name` field automatically. The
`set-viber-name` command records a name you have verified in Viber; it does not
rename the business or Android contact. A successful background `open` also
records the name shown in Viber when it differs from the business and Android
contact names. Unknown names remain empty.

## Android contact behavior

`add-contact` normalizes a Serbian number to `+381...`, reserves the next ID,
then uses ADB's Contacts Provider commands to insert a raw contact with a unique
`sync1` marker, followed by a name and phone data row. Android's `content insert`
command can print nothing on success, so the CLI queries that marker
to find the new raw-contact ID. It reads the name and phone rows back before
committing the SQLite lead.
If insertion or verification fails, it reports an error and rolls back the
local row. It attempts to remove a partially inserted Android raw contact. If
Android does not expose the marker after insertion, the error includes the
marker because an empty raw contact may remain on the device.

Android builds can deny Contacts Provider access to the ADB shell. In that case
this version cannot add a contact on that phone and will fail explicitly. A
device-side helper with Contacts permission or a verified Contacts-app UI
workflow would be the next adapter; launching a prefilled edit screen alone
would not prove the contact was saved. A rare SQLite commit failure after a
successful Android insertion could leave an Android contact without a local
lead; check the phone if such an error occurs. The provider's batch API would
make Android row creation atomic in a future device-side implementation.

## Viber accessibility limits

The adapter uses `pywinauto` with the `uia` backend to locate controls. The
background path posts mouse messages directly to Viber's window and uses OCR
on a captured header. It does not use screen coordinates, the system pointer,
or the clipboard. If the controls or header differ on another Viber version,
the command stops. Run `inspect` and update the isolated selectors in
`app/viber_background.py` for that Viber version.

`read` reports text elements visible in the current conversation viewport.
It does not scroll to fetch old history. If explicit accessibility metadata
does not identify message direction, output uses `MESSAGE` rather than a
guess. Viber may expose dates or other chat text as message elements, or may
expose no usable message elements at all. Compare initial read output with the
visible chat before relying on it. Some Viber Qt/QML builds expose message
contents as UIA `Edit` values but omit the conversation header entirely.
`read-current` still prints visible messages in that case and labels the contact
as unavailable. `open`, `read <id>`, and `send` stop when background header OCR
or the dial-pad controls cannot verify a named chat. Viber's dial pad has both
Call and Message buttons on the tested build; the CLI uses only Message.
OCR can misread unusual display names. Check the stored `viber_name` in
`contacts` and correct it with `set-viber-name` if needed. The CLI will stop if
a later chat header differs from the stored name.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover phone normalization, ID extraction, database rollback, and the
send confirmation and header gate. The background dial pad and name capture
were also tested against the installed Viber Desktop without sending a live
message. Final delivery behavior remains unverified until an explicitly
confirmed send is performed.

References: [Android ADB](https://developer.android.com/tools/adb),
[Contacts Provider](https://developer.android.com/identity/providers/contacts-provider),
[Android `content` command source](https://android.googlesource.com/platform/frameworks/base/+/6589d834619d/cmds/content/src/com/android/commands/content/Content.java),
[pywinauto UIA](https://pywinauto.readthedocs.io/en/latest/getting_started.html).
