"""Focus-free Viber Desktop control for the tested Qt dial pad.

Mouse and character messages are posted to Viber's own window. They never use
the system pointer, keyboard, or clipboard. Every number entry is read back;
the chat name is read from a PrintWindow capture because this Viber build does
not expose its header through UI Automation.
"""

import ctypes
from ctypes import wintypes
from contextlib import nullcontext
import re
import time

from app.viber import ViberClient, ViberError
from app.reply_errors import ViberRetryable


_USER32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_VK_BACK = 0x08
_PAD = "123456789*0#"


def _usable_viber_name(name: str) -> bool:
    return bool(name and name not in {"Unknown", "My Notes"} and
                not re.search(r"\b(?:No results|not on Viber)\b", name, re.I) and
                not any(ord(char) < 32 for char in name) and len(name) <= 120)


class BackgroundViberClient:
    def __init__(self, debug: bool = False, allow_foreground: bool = False):
        self.ui = ViberClient(debug=debug)
        self.allow_foreground = allow_foreground
        self.window = None
        self.expected_name: str | None = None
        self.prepared = None

    def connect(self):
        if _USER32 is None:
            raise ViberError("Background Viber control requires Windows.")
        self.ui.connect()
        self.window = self.ui.window
        if _USER32.IsIconic(self.window.handle):
            if not self.allow_foreground:
                raise ViberError("Restore Viber behind your other windows before using background mode.")
            _USER32.ShowWindow(self.window.handle, 9)  # Restore only inside the isolated VM.
            deadline=time.monotonic()+2
            while _USER32.IsIconic(self.window.handle):
                if time.monotonic()>=deadline:
                    raise ViberRetryable('Viber could not be restored in its VM.', 'window_minimized')
                time.sleep(.05)
        return self

    def _nodes(self):
        return self.ui._nodes()

    def _one(self, label: str, predicate):
        matches = [node for node in self._nodes() if predicate(node)]
        if len(matches) != 1:
            error = ViberRetryable if not matches else ViberError
            raise error(f"Expected one {label}; found {len(matches)}. No message was sent.")
        return matches[0]

    def _assert_no_focus_theft(self):
        if self.allow_foreground:
            return
        foreground = _USER32.GetForegroundWindow()
        foreground_pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
        viber_pid = self.window.process_id()
        if foreground_pid.value == viber_pid:
            raise ViberError("Background mode requires Viber to stay behind another app.")

    def _post(self, message: int, wparam: int, lparam: int = 1) -> None:
        if not _USER32.PostMessageW(self.window.handle, message, wparam, lparam):
            raise ViberError("Windows rejected a background Viber input message.")

    def _click(self, node, hold: float = 0) -> None:
        rect = node.rectangle()
        point = wintypes.POINT((rect.left + rect.right) // 2,
                               (rect.top + rect.bottom) // 2)
        if not _USER32.ScreenToClient(self.window.handle, ctypes.byref(point)):
            raise ViberError("Windows could not map the Viber control to its client area.")
        client = wintypes.RECT()
        if not _USER32.GetClientRect(self.window.handle, ctypes.byref(client)):
            raise ViberError("Windows could not read the Viber client area.")
        x, y = point.x, point.y
        if not (client.left <= x < client.right and client.top <= y < client.bottom):
            raise ViberError("Viber control is outside the main window.")
        packed_point = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        self._post(_WM_MOUSEMOVE, 0, packed_point)
        self._post(_WM_LBUTTONDOWN, 1, packed_point)
        if hold:
            time.sleep(hold)
        self._post(_WM_LBUTTONUP, 0, packed_point)
        time.sleep(.07)
        self._assert_no_focus_theft()

    def _dismiss_obscuring_info_popup(self) -> None:
        """Close a stale contact/group info sheet before opening the dial pad."""
        nodes = self._nodes()
        if not any("InfoPopup" in self.ui._auto_id(node) for node in nodes):
            return
        closes = [node for node in nodes
                  if self.ui._type(node) == "Button" and
                  re.search(r"InfoPopup\.IconButton_", self.ui._auto_id(node)) and
                  "SideBarContent" not in self.ui._auto_id(node)]
        if len(closes) != 1:
            raise ViberError(
                "Viber has an open info panel that could not be closed safely. No message was sent.")
        self._click(closes[0])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not any("InfoPopup" in self.ui._auto_id(node) for node in self._nodes()):
                return
            time.sleep(.1)
        raise ViberError("Viber's open info panel did not close. No message was sent.")

    def _capture_header(self):
        if _USER32.IsIconic(self.window.handle):
            raise ViberError("Viber was minimized; background header verification stopped.")
        try:
            import win32gui
            import win32ui
            from PIL import Image
        except ImportError as exc:
            raise ViberError("Install requirements.txt for background Viber capture.") from exc
        pane = self._one("conversation pane", lambda n:
                         self.ui._type(n) == "Pane" and
                         "StackView_QMLTYPE_" in self.ui._auto_id(n) and
                         n.rectangle().left > self.window.rectangle().left +
                         self.window.rectangle().width() * .2 and
                         n.rectangle().width() > 250)
        win_rect = self.window.rectangle()
        width, height = win_rect.width(), win_rect.height()
        hwnd_dc = win32gui.GetWindowDC(self.window.handle)
        source = win32ui.CreateDCFromHandle(hwnd_dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        try:
            if not _USER32.PrintWindow(self.window.handle, memory.GetSafeHdc(), 2):
                raise ViberError("Could not capture the Viber window in background.")
            info = bitmap.GetInfo()
            image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                     bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
            pane_rect = pane.rectangle()
            left = pane_rect.left - win_rect.left + 75
            top = pane_rect.top - win_rect.top + 45
            searches = [n for n in self._nodes() if self.ui._type(n) == "Button" and
                        "SearchButton_QMLTYPE_" in self.ui._auto_id(n) and
                        pane_rect.left <= n.rectangle().left < pane_rect.right]
            right = (searches[0].rectangle().left - win_rect.left - 12
                     if len(searches) == 1 else left + 200)
            if right <= left + 80 or top + 30 > height:
                raise ViberError("Viber header area is too small for verification.")
            return image.crop((left, top, right, top + 30)).resize(
                ((right - left) * 5, 150))
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(self.window.handle, hwnd_dc)

    def current_name(self) -> str:
        try:
            import pytesseract
            name = pytesseract.image_to_string(
                self._capture_header(), lang="eng+srp_latn", config="--psm 7").strip()
        except (OSError, RuntimeError) as exc:
            raise ViberRetryable(f"Could not read the background Viber header: {exc}", 'header_unavailable') from exc
        self._assert_no_focus_theft()
        return " ".join(name.split())

    def verify_current_name(self, expected: str) -> bool:
        if self.current_name() != expected:
            return False
        time.sleep(.12)
        return self.current_name() == expected

    def _dial(self, phone: str) -> None:
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise ViberError("A full international phone number is required.")
        self._dismiss_obscuring_info_popup()
        popup = self._open_dial_pad()
        children = popup.children()
        fields = [n for n in children if self.ui._type(n) == "Edit"]
        keys = sorted((n for n in children if self.ui._type(n) == "Button" and
                       "RoundIconButton_" in self.ui._auto_id(n)),
                      key=lambda n: (n.rectangle().top, n.rectangle().left))
        if len(fields) != 1 or len(keys) != 12:
            raise ViberRetryable("Dial pad controls changed; no message was sent.", 'dial_pad_not_ready')
        field = fields[0]
        if field.get_value():
            clear = [n for n in children if self.ui._type(n) == "Button" and
                     "IconButton_QMLTYPE_57" in self.ui._auto_id(n)]
            if len(clear) != 1:
                raise ViberError("Could not clear the dial pad; no message was sent.")
            self._click(clear[0])
        if not self._wait_value(field, '', timeout=.5):
            raise ViberRetryable("Dial pad was not cleared; no message was sent.", 'dial_pad_readback')
        self._click(keys[10], hold=1.2)  # Long press 0 enters +.
        if not self._wait_value(field, '+', timeout=.5):
            raise ViberRetryable("Could not enter the international prefix.", 'dial_pad_readback')
        keymap = dict(zip(_PAD, keys))
        for index, digit in enumerate(phone[1:], 1):
            self._click(keymap[digit])
            if not self._wait_value(field, phone[:index + 1], timeout=.5):
                raise ViberRetryable("Dial pad number differed from the lead; no message was sent.", 'dial_pad_readback')
        actions = sorted((n for n in children if self.ui._type(n) == "Button" and
                          "SmallIconButton_" in self.ui._auto_id(n)),
                         key=lambda n: n.rectangle().left)
        if len(actions) != 2 or not actions[1].is_enabled():
            raise ViberRetryable("The dial pad message button is unavailable.", 'dial_pad_not_ready')
        self._click(actions[1])  # Right button is Message on the tested build.

    def _open_dial_pad(self):
        """Resume an existing menu/dial pad instead of toggling its hidden button.

        Qt can expose only popup controls while the profile menu is open. A
        previous interrupted recipient-open must not require the obscured
        profile button again. Every later number/recipient check still runs.
        """
        deadline = time.monotonic() + 3
        clicked_profile = False
        clicked_dial = False
        while time.monotonic() < deadline:
            nodes = self._nodes()
            pads = [n for n in nodes if self.ui._auto_id(n).endswith('ProfilePopup')
                    and any(self.ui._type(child) == 'Edit' for child in n.children())]
            if len(pads) > 1:
                raise ViberError('Multiple Viber dial pads found. No message was sent.')
            if pads:
                return pads[0]
            dials = [n for n in nodes if self.ui._type(n) == 'Button'
                     and self.ui._name(n) == 'Use dial pad']
            if len(dials) > 1:
                raise ViberError('Multiple Use dial pad buttons found. No message was sent.')
            if dials:
                if not clicked_dial:
                    self._click(dials[0])
                    clicked_dial = True
                time.sleep(.1)
                continue
            profiles = [n for n in nodes if self.ui._type(n) in ('CheckBox', 'Button')
                        and 'ProfileButton_' in self.ui._auto_id(n)]
            if len(profiles) > 1:
                raise ViberError('Multiple Viber profile buttons found. No message was sent.')
            if profiles and not clicked_profile and not clicked_dial:
                self._click(profiles[0])
                clicked_profile = True
            time.sleep(.1)
        raise ViberRetryable('Viber dial pad is not ready. Waiting before reopening the verified number. No message was sent.', 'dial_pad_not_ready')

    def open_phone(self, phone: str) -> str:
        if self.window is None:
            self.connect()
        self._assert_no_focus_theft()
        self._dial(phone)
        time.sleep(1)
        deadline = time.monotonic() + 9
        previous = None
        repeats = 0
        while time.monotonic() < deadline:
            time.sleep(.35)
            if any(self.ui._auto_id(n).endswith("ProfilePopup") for n in self._nodes()):
                continue
            name = self.current_name()
            if name == previous and _usable_viber_name(name):
                repeats += 1
                if repeats >= 2:
                    self.expected_name = name
                    return name
            else:
                repeats = 0
                previous = name
        raise ViberRetryable("Viber did not expose a stable named chat for that number. No message was sent.", 'header_unstable')

    def read_messages(self):
        return self.ui.read_messages()

    def _erase_draft(self, length: int) -> None:
        for _ in range(length + 2):
            self._post(_WM_KEYDOWN, _VK_BACK)
            self._post(_WM_CHAR, _VK_BACK)
            self._post(_WM_KEYUP, _VK_BACK)
        time.sleep(.15)

    @staticmethod
    def _wait_value(composer, expected, timeout=2):
        deadline = time.monotonic() + timeout
        while True:
            value = composer.get_value()
            if value == expected:
                return True
            # Wait only for our own prefix to finish arriving. Foreign text is
            # not treated as a slow frame and is never silently overwritten.
            if expected and not expected.startswith(value):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)

    def _clear_owned_draft(self, expected, text, composer, length):
        if not self.verify_current_name(expected):
            raise ViberError('Recipient changed while preparing text; the draft was left unsent for review.')
        value = composer.get_value()
        if value and not text.startswith(value):
            raise ViberError('Draft contains unexpected text; it was left unsent for review.')
        # Erase all posted characters, including those still in Qt's queue.
        self._erase_draft(length)
        if not self._wait_value(composer, ''):
            raise ViberError('The unsent draft could not be cleared. Check Viber before retrying.')

    def prepare_message(self, text: str) -> None:
        """Type and verify a draft without pressing Send."""
        if self.prepared is not None:
            raise ViberError("Another verified draft is already waiting to be sent.")
        if not text or not text.strip() or "\n" in text or "\r" in text:
            raise ViberError("Send one nonempty line at a time.")
        if any(ord(char) < 32 or ord(char) > 0xFFFF for char in text):
            raise ViberError("Background typing supports plain text without controls or emoji.")
        if not self.expected_name or not self.verify_current_name(self.expected_name):
            raise ViberRetryable("Conversation header changed before typing. No message was sent.", 'header_unstable')
        composer = self.ui._composer()
        if composer.get_value():
            raise ViberError("Viber already has an unsent draft; no message was sent.")
        units = text.encode("utf-16-le")
        failure_code = 'draft_prepare_failed'
        try:
            self._click(composer)
            for offset in range(0, len(units), 2):
                self._post(_WM_CHAR, int.from_bytes(units[offset:offset + 2], "little"))
            failure_code = 'draft_readback'
            if not self._wait_value(composer, text):
                raise ViberError('Typed text did not match the requested reply.')
            failure_code = 'header_unstable'
            if not self.verify_current_name(self.expected_name):
                raise ViberError('Recipient header was unstable after typing.')
            failure_code = 'send_control_not_ready'
            send = self._one("message send button", lambda n:
                             self.ui._type(n) == "Button" and
                             self.ui._auto_id(n).endswith("SendToolbarButton"))
            if not send.is_enabled():
                raise ViberError('Viber send button is not ready.')
            self._assert_no_focus_theft()
        except Exception as exc:
            self._clear_owned_draft(self.expected_name, text, composer, len(units) // 2)
            raise ViberRetryable(f'{exc} Owned draft cleared; no Send click occurred.', failure_code) from exc
        self.prepared = (self.expected_name, text, composer, send, len(units) // 2)

    def cancel_prepared(self) -> None:
        if self.prepared is None:
            return
        expected, text, composer, _send, length = self.prepared
        self._clear_owned_draft(expected, text, composer, length)
        self.prepared = None

    def dispatch_prepared(self) -> None:
        if self.prepared is None:
            raise ViberError("No verified draft is waiting to be sent.")
        expected, text, composer, send, length = self.prepared
        if not self.verify_current_name(expected) or composer.get_value() != text:
            if self.verify_current_name(expected) and composer.get_value() == text:
                self._erase_draft(length)
            self.prepared = None
            raise ViberError("Conversation or draft changed before dispatch.")
        self._click(send)
        time.sleep(.3)
        if composer.get_value():
            raise ViberError("Viber did not clear the draft; delivery is unverified.")
        self.prepared = None

    def send_message(self, text: str, before_dispatch=None, on_dispatch=None, dispatch_lock=None) -> None:
        self.prepare_message(text)
        try:
            if before_dispatch:
                before_dispatch()
            with dispatch_lock if dispatch_lock is not None else nullcontext():
                if on_dispatch:
                    on_dispatch()
                self.dispatch_prepared()
        except Exception:
            self.cancel_prepared()
            raise
