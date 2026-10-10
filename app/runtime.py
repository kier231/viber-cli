"""Local configuration; never print credentials or inherit them into Codex."""
import os
from pathlib import Path


def settings(root=None):
    root = Path(root or Path(__file__).resolve().parent.parent)
    result = {}
    path = root / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                result[key] = value.strip()
    for key in ('VIBER_DATABASE_URL','VIBER_POSTGRES_PASSWORD'):
        if os.environ.get(key):
            result[key] = os.environ[key]
    return result
