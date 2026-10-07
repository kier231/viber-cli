"""Manually triggered Windows console commands for Viber Desktop."""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

from app.android_contacts import AndroidContactError, add_android_contact
from app.models import LeadStore
from app.phone import extract_lead_id, normalize_serbian_phone
from app.viber import ViberClient, ViberError


DEFAULT_DB = Path(__file__).resolve().parent / "leads.sqlite3"


def _lead(store: LeadStore, lead_id: int):
    lead = store.get(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} does not exist.")
    return lead


def _open_verified(client: ViberClient, lead) -> str:
    print(f"Opening {lead.contact_name}...")
    client.connect()
    try:
        client.search_contact(lead.contact_name)
    except ViberError as exc:
        raise ViberError(f"Could not verify conversation: {exc} No message was sent.") from exc
    try:
        detected = client.get_current_contact_name()
    except ViberError as exc:
        raise ViberError("Could not verify conversation. No message was sent.") from exc
    print(f"Expected: SJT-{lead.id}")
    print(f"Detected: {detected}")
    if not client.verify_contact(lead.id) or detected != lead.contact_name:
        raise ViberError("Could not verify conversation. No message was sent.")
    print("VERIFIED")
    return detected


def _print_messages(messages) -> None:
    print("-" * 50)
    if not messages:
        print("No visible message text was exposed through UI Automation.")
    for message in messages:
        print(f"{message.direction}:\n{message.text}\n")
    print("-" * 50)


def _confirm_send(client: ViberClient, lead, message: str) -> bool:
    print(f"\nContact:\n{lead.contact_name}\n")
    print(f"Phone:\n{lead.phone}\n")
    print(f"Message:\n{message}\n")
    if input("Send? [y/N]: ") != "y":
        print("Cancelled. No message was sent.")
        return False
    # send_message performs a fresh search and header verification after input.
    client.send_message(message)
    print("SENT (Enter dispatched to Viber; delivery is not verified).")
    return True


def dispatch(args, store: LeadStore, client_factory=ViberClient,
             android_add=add_android_contact) -> None:
    if args.command == "add-contact":
        phone = normalize_serbian_phone(args.phone)
        lead = store.create_with_android(phone, args.company, android_add)
        print(f"Created lead ID: {lead.id}")
        print(f"Android contact: {lead.contact_name}")
        return
    if args.command == "contacts":
        leads = store.all()
        widths = (max([2] + [len(str(x.id)) for x in leads]),
                  max([7] + [len(x.company_name) for x in leads]),
                  max([5] + [len(x.phone) for x in leads]))
        print(f"{'ID':<{widths[0]}}  {'Company':<{widths[1]}}  "
              f"{'Phone':<{widths[2]}}  Viber name")
        for lead in leads:
            print(f"{lead.id:<{widths[0]}}  {lead.company_name:<{widths[1]}}  "
                  f"{lead.phone:<{widths[2]}}  {lead.contact_name}")
        return
    client = client_factory(debug=args.debug)
    if args.command == "inspect":
        client.connect()
        client.inspect()
    elif args.command == "read-current":
        client.connect()
        try:
            name = client.get_current_contact_name()
        except ViberError:
            name = None
        lead_id = extract_lead_id(name) if name else None
        lead = store.get(lead_id) if lead_id else None
        print(f"Contact:\n{name or 'Unavailable through UI Automation'}\n")
        print(f"Detected lead:\n{lead_id if lead and lead.contact_name == name else 'none'}\n")
        print("Messages:")
        _print_messages(client.read_messages())
    elif args.command in {"open", "send", "read", "chat"}:
        lead = _lead(store, args.lead_id)
        _open_verified(client, lead)
        if args.command == "open":
            print("Conversation verified.\nReady.")
        elif args.command == "send":
            _confirm_send(client, lead, args.message)
        elif args.command == "read":
            print(f"Conversation:\n{lead.contact_name}\n")
            _print_messages(client.read_messages())
        else:
            print(f"Opened:\n{lead.contact_name}\n")
            while True:
                line = input("Type message, /read, or /exit:\n> ")
                if line == "/exit":
                    break
                if line == "/read":
                    if not client.verify_contact(lead.id):
                        raise ViberError("Conversation changed; /read aborted.")
                    _print_messages(client.read_messages())
                    continue
                if not line.strip():
                    continue
                _confirm_send(client, lead, line)
    else:
        raise ValueError(f"Unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="viber-cli")
    parser.add_argument("--debug", action="store_true", help="Print UI selector diagnostics")
    subs = parser.add_subparsers(dest="command")
    subs.add_parser("inspect", help="Print the Viber UI Automation hierarchy")
    add = subs.add_parser("add-contact", help="Add a verified Android contact")
    add.add_argument("phone")
    add.add_argument("company")
    subs.add_parser("contacts", help="List locally stored leads")
    for name in ("open", "read", "chat", "send"):
        sub = subs.add_parser(name)
        sub.add_argument("lead_id", type=int)
        if name == "send":
            sub.add_argument("message")
    subs.add_parser("read-current", help="Read whichever Viber chat is open")
    return parser


def _menu(parser, store: LeadStore, debug: bool) -> None:
    commands = {
        "1": lambda: ["add-contact", input("Phone: "), input("Company: ")],
        "2": lambda: ["contacts"],
        "3": lambda: ["open", input("Lead ID: ")],
        "4": lambda: ["send", input("Lead ID: "), input("Message: ")],
        "5": lambda: ["read", input("Lead ID: ")],
        "6": lambda: ["chat", input("Lead ID: ")],
        "7": lambda: ["inspect"],
    }
    while True:
        print("\nVIBER CLI\n\n1. Add contact\n2. List contacts\n"
              "3. Open conversation\n4. Send message\n5. Read conversation\n"
              "6. Open interactive chat\n7. Inspect Viber controls\n8. Exit\n")
        choice = input("Choose: ").strip()
        if choice == "8":
            return
        if choice not in commands:
            print("Invalid choice.")
            continue
        try:
            parts = commands[choice]()
            parsed = parser.parse_args((["--debug"] if debug else []) + parts)
            dispatch(parsed, store)
        except (ValueError, AndroidContactError, ViberError, sqlite3.Error) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # PowerShell redirection can give Python a legacy Windows output encoding.
    # Chat text may contain Serbian diacritics or emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = _parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept --debug before or after the subcommand.
    debug = "--debug" in argv
    argv = [arg for arg in argv if arg != "--debug"]
    args = parser.parse_args((["--debug"] if debug else []) + argv)
    store = LeadStore(os.environ.get("VIBER_CLI_DB", DEFAULT_DB))
    try:
        if args.command is None:
            _menu(parser, store, args.debug)
        else:
            dispatch(args, store)
    except (ValueError, AndroidContactError, ViberError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
