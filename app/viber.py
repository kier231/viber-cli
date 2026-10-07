"""Viber Desktop adapter using Windows UI Automation through pywinauto.

Viber's accessible tree depends on its installed version. Ambiguous selectors
raise errors instead of choosing a recipient or composer by guesswork.
"""

import re
import sys
import time

from app.models import Message
from app.phone import extract_lead_id


class ViberError(RuntimeError):
    pass


class ViberClient:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.window = None
        self._expected_name: str | None = None

    def _debug(self, message: str) -> None:
        if self.debug:
            print(f"[DEBUG] {message}", file=sys.stderr)

    @staticmethod
    def _type(node) -> str:
        return node.element_info.control_type or ""

    @staticmethod
    def _name(node) -> str:
        return (node.element_info.name or "").strip()

    @staticmethod
    def _auto_id(node) -> str:
        return node.element_info.automation_id or ""

    @staticmethod
    def _center(node) -> tuple[int, int]:
        rect = node.rectangle()
        return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

    def _nodes(self) -> list:
        if self.window is None:
            self.connect()
        try:
            return self.window.descendants()
        except Exception as exc:
            raise ViberError(f"Could not inspect Viber UI Automation tree: {exc}") from exc

    def connect(self):
        try:
            import psutil
            from pywinauto import Desktop
        except ImportError as exc:
            raise ViberError("Install requirements.txt before using Viber commands.") from exc
        pids = {p.info["pid"] for p in psutil.process_iter(["pid", "name"])
                if (p.info["name"] or "").lower() == "viber.exe"}
        if not pids:
            raise ViberError("Viber Desktop is not running.")
        try:
            windows = [w for w in Desktop(backend="uia").windows()
                       if w.process_id() in pids and w.is_visible()]
            windows = [w for w in windows if w.rectangle().width() > 300 and
                       w.rectangle().height() > 200]
        except Exception as exc:
            raise ViberError(f"Could not enumerate Viber windows: {exc}") from exc
        if len(windows) != 1:
            raise ViberError(f"Expected one main Viber window; found {len(windows)}.")
        self.window = windows[0]
        self._debug(f"Found Viber window: {self._name(self.window)!r}")
        return self

    def inspect(self) -> None:
        """Print the complete accessible subtree with names, types and IDs."""
        if self.window is None:
            self.connect()
        def walk(node, depth: int) -> None:
            try:
                print(f"{'  ' * depth}{self._type(node):<16} "
                      f"name={self._name(node)!r} auto_id={self._auto_id(node)!r}")
                for child in node.children():
                    walk(child, depth + 1)
            except Exception as exc:
                print(f"{'  ' * depth}<inaccessible: {exc}>")
        walk(self.window, 0)

    def _search_edit(self):
        win = self.window.rectangle()
        edits = [n for n in self._nodes() if self._type(n) == "Edit" and
                 "search" in (self._name(n) + " " + self._auto_id(n)).lower() and
                 self._center(n)[0] < win.left + win.width() * .6]
        if len(edits) == 1:
            self._debug(f"Search control: {self._type(edits[0])}")
            return edits[0]
        if len(edits) > 1:
            raise ViberError("Multiple Viber search fields found; inspect selectors.")
        buttons = [n for n in self._nodes() if self._type(n) == "Button" and
                   "search" in (self._name(n) + " " + self._auto_id(n)).lower() and
                   self._center(n)[0] < win.left + win.width() * .6]
        if len(buttons) == 1:
            buttons[0].click_input()
            time.sleep(.2)
            edits = [n for n in self._nodes() if self._type(n) == "Edit" and
                     self._center(n)[0] < win.left + win.width() * .6 and
                     self._center(n)[1] < win.top + win.height() * .35]
            if len(edits) == 1:
                return edits[0]
        raise ViberError("A unique accessible Viber search field was not found. Run inspect.")

    def search_contact(self, name: str) -> None:
        """Search for and open exactly one matching UIA result."""
        if self.window is None:
            self.connect()
        from pywinauto.keyboard import send_keys
        import pyperclip
        self.window.set_focus()
        search = self._search_edit()
        search.click_input()
        pyperclip.copy(name)
        send_keys("^a")
        send_keys("^v")
        self._debug(f"Searching for: {name}")
        win = self.window.rectangle()
        search_bottom = search.rectangle().bottom
        deadline = time.monotonic() + 6
        matches = []
        while time.monotonic() < deadline:
            matches = [n for n in self._nodes()
                       if self._type(n) in ("Text", "ListItem") and
                       self._name(n) == name and
                       self._center(n)[0] < win.left + win.width() * .45 and
                       self._center(n)[1] > search_bottom]
            # A ListItem may duplicate its Text child. Prefer the ListItem.
            items = [n for n in matches if self._type(n) == "ListItem"]
            if items:
                matches = items
            if matches:
                break
            time.sleep(.2)
        self._debug(f"Found {len(matches)} result(s)")
        if len(matches) != 1:
            raise ViberError(f"Expected one exact search result for {name!r}; found {len(matches)}.")
        matches[0].click_input()
        self._expected_name = name
        time.sleep(.3)

    def _header(self):
        """Identify a conversation title in the upper part of the right pane."""
        win = self.window.rectangle()
        candidates = [n for n in self._nodes()
                      if self._type(n) in ("Text", "Header") and self._name(n) and
                      self._center(n)[0] > win.left + win.width() * .5 and
                      self._center(n)[1] < win.top + win.height() * .18 and
                      extract_lead_id(self._name(n)) is not None]
        # A duplicate control with the same name is still ambiguous without
        # knowing which is the actual header.
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ViberError("Ambiguous Viber conversation header; run inspect.")
        # For read-current on a non-SJT chat, only accept an explicitly named
        # header control; never choose arbitrary text near the top of the pane.
        explicit = [n for n in self._nodes()
                    if self._type(n) in ("Text", "Header") and self._name(n) and
                    "header" in self._auto_id(n).lower() and
                    self._center(n)[0] > win.left + win.width() * .5 and
                    self._center(n)[1] < win.top + win.height() * .18]
        if len(explicit) == 1:
            return explicit[0]
        raise ViberError("Could not identify the current conversation header. Run inspect.")

    def get_current_contact_name(self) -> str:
        if self.window is None:
            self.connect()
        name = self._name(self._header())
        self._debug(f"Header: {name}")
        return name

    def verify_contact(self, lead_id: int) -> bool:
        try:
            detected = self.get_current_contact_name()
        except ViberError:
            return False
        valid = extract_lead_id(detected) == lead_id and (
            self._expected_name is None or detected == self._expected_name)
        self._debug("Verification successful" if valid else "Verification failed")
        return valid

    def _composer(self):
        win = self.window.rectangle()
        edits = [n for n in self._nodes() if self._type(n) == "Edit" and
                 self._center(n)[0] > win.left + win.width() * .5 and
                 self._center(n)[1] > win.top + win.height() * .65]
        if len(edits) != 1:
            raise ViberError(f"Expected one accessible message composer; found {len(edits)}.")
        return edits[0]

    def send_message(self, text: str) -> None:
        """Re-search and re-verify immediately before dispatching one message."""
        if not text or not text.strip() or "\n" in text or "\r" in text:
            raise ViberError("Send one nonempty line at a time.")
        if not self._expected_name:
            raise ViberError("Search for a lead before sending.")
        lead_id = extract_lead_id(self._expected_name)
        if lead_id is None:
            raise ViberError("Expected contact has no unique SJT tag.")
        self.search_contact(self._expected_name)
        if not self.verify_contact(lead_id):
            raise ViberError("ABORTING SEND: Could not verify conversation.")
        composer = self._composer()
        composer.click_input()
        import pyperclip
        from pywinauto.keyboard import send_keys
        pyperclip.copy(text)
        send_keys("^v")
        # A chat switch between focus and paste must never be followed by Enter.
        if not self.verify_contact(lead_id):
            try:
                send_keys("^a{BACKSPACE}")
            finally:
                raise ViberError("ABORTING SEND: Header changed after paste.")
        send_keys("{ENTER}")

    def read_messages(self) -> list[Message]:
        """Return visible UIA text from the conversation body only.

        Direction is emitted only when an ancestor has explicit accessibility
        metadata saying incoming/outgoing. Scrolling history is out of scope.
        """
        header = self._header()
        win = self.window.rectangle()
        try:
            bottom = self._composer().rectangle().top
        except ViberError:
            bottom = win.bottom - win.height() * .15
        top = header.rectangle().bottom
        messages = []
        for node in self._nodes():
            if self._type(node) != "Text":
                continue
            value = self._name(node)
            if not value:
                continue
            x, y = self._center(node)
            if x <= win.left + win.width() * .5 or not top < y < bottom:
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}(?:\s*[AP]M)?", value, re.I):
                continue
            direction = "MESSAGE"
            parent = node
            for _ in range(3):
                try:
                    parent = parent.parent()
                    label = self._name(parent).lower()
                except Exception:
                    break
                if re.search(r"\b(?:outgoing|sent message)\b", label):
                    direction = "ME"
                    break
                if re.search(r"\b(?:incoming|received message)\b", label):
                    direction = "THEM"
                    break
            messages.append((node.rectangle().top, node.rectangle().left,
                             Message(value, direction)))
        messages.sort(key=lambda item: (item[0], item[1]))
        return [message for _, _, message in messages]
