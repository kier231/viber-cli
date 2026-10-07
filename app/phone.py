"""Pure Serbian phone-number and lead-tag helpers."""

import re


def normalize_serbian_phone(value: str) -> str:
    """Return an E.164 Serbian number, rejecting ambiguous inputs."""
    if not isinstance(value, str):
        raise ValueError("Phone number must be text.")
    cleaned = re.sub(r"[\s().-]", "", value)
    if not re.fullmatch(r"\+?\d+", cleaned):
        raise ValueError("Phone number contains unsupported characters.")
    if cleaned.startswith("+381"):
        national = cleaned[4:]
    elif cleaned.startswith("381"):
        national = cleaned[3:]
    elif cleaned.startswith("0"):
        national = cleaned[1:]
    else:
        raise ValueError("Use a Serbian number beginning with 0, 381, or +381.")
    if not re.fullmatch(r"[1-9]\d{7,9}", national):
        raise ValueError("Serbian national number must contain 8 to 10 digits.")
    return "+381" + national


def extract_lead_id(contact_name: str) -> int | None:
    """Extract one unambiguous SJT tag from a contact name."""
    if not isinstance(contact_name, str):
        return None
    matches = re.findall(r"(?<![\w-])SJT-([1-9]\d*)(?![\w-])", contact_name)
    return int(matches[0]) if len(matches) == 1 else None
