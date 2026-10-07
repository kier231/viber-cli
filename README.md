# viber-cli

A manual Windows console tool for keeping an Android contact ledger and opening,
sending to, or reading Viber Desktop chats. The default chat path enters a phone
number on Viber's dial pad and presses its message button without moving the
Windows mouse or taking keyboard focus. There are no workers, schedules, AI
features, automatic replies, or message queues.

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
