# viber-cli

A manual Windows console tool for keeping an Android contact ledger and opening,
sending to, or reading the currently visible Viber Desktop conversation. There
are no workers, schedules, AI features, automatic replies, or message queues.

## Requirements

- Windows 11 and Python 3.12 or newer.
- Viber Desktop installed, running, logged in, and displaying its normal main window.
- Android Platform Tools (`adb`), USB debugging authorized, and exactly one
  usable device connected. The CLI checks `PATH`, common Windows install paths
  (including `C:\adb\platform-tools`), and `VIBER_CLI_ADB`.
- Viber on the phone; contacts must sync to Viber Desktop before search works.
- A foreground, unlocked Windows session. UI Automation and clipboard paste
  need the desktop; they cannot run in a disconnected background session.

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
by version; this project has not been tested against a live Viber instance yet.

## Commands

```powershell
.\.venv\Scripts\python.exe main.py add-contact "+381641234567" "Auto Servis Markovic"
.\.venv\Scripts\python.exe main.py contacts
.\.venv\Scripts\python.exe main.py open 1
.\.venv\Scripts\python.exe main.py send 1 "Pozdrav, hteo sam nesto da vas pitam."
.\.venv\Scripts\python.exe main.py read 1
.\.venv\Scripts\python.exe main.py chat 1
.\.venv\Scripts\python.exe main.py read-current
.\.venv\Scripts\python.exe main.py --debug inspect
.\.venv\Scripts\python.exe main.py
```

`chat` accepts `/read` and `/exit`. The menu calls the same command functions.
Only a literal lowercase `y` at a send prompt authorizes sending. Each send
searches again, opens the unique matching result, and checks the exact
conversation name and `SJT-{id}` tag before paste and again before Enter.
Messages are pasted through the clipboard, one line at a time. `SENT` means
Enter was dispatched to Viber; it is not a delivery receipt.

The SQLite database is `leads.sqlite3` next to `main.py`. Set `VIBER_CLI_DB` to
an alternate path. The database stores only `id`, `phone`, `company_name`,
`contact_name`, and `created_at`. It does not contain messages.

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

The adapter uses `pywinauto` with the `uia` backend. It uses accessible control
names/types and relative positions inside the Viber window, with no OCR or
fixed screen coordinates. If a unique search result, header, or composer is
not accessible, the command stops. Run `inspect` and update the isolated
selectors in `app/viber.py` for that Viber version.

`read` reports text elements visible in the current conversation viewport.
It does not scroll to fetch old history. If explicit accessibility metadata
does not identify message direction, output uses `MESSAGE` rather than a
guess. Viber may expose dates or other chat text as message elements, or may
expose no usable message elements at all. Compare initial read output with the
visible chat before relying on it. `read-current` can identify an SJT-tagged
chat or a non-tagged chat with an explicit header automation ID; it otherwise
fails rather than naming the wrong chat.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover phone normalization, ID extraction, database rollback, and the
send confirmation and header gate. Live Viber/Android integration remains to
be validated on the target devices, in the requested phase order.

References: [Android ADB](https://developer.android.com/tools/adb),
[Contacts Provider](https://developer.android.com/identity/providers/contacts-provider),
[Android `content` command source](https://android.googlesource.com/platform/frameworks/base/+/6589d834619d/cmds/content/src/com/android/commands/content/Content.java),
[pywinauto UIA](https://pywinauto.readthedocs.io/en/latest/getting_started.html).
