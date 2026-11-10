"""Block a commit that would add secrets, personal data or databases.

Run by the pre-commit hook on the files being committed. It refuses:
- .env files (anything named .env or .env.* except .env.example)
- local settings (chaosmonkey.toml, fleet.toml); commit the .example files instead
- key and certificate files (*.key, *.pem)
- anything under data/
- database files
- files over 1 MB
- text containing something that looks like a key or token

Exit code 1 stops the commit and prints why.
"""

import re
import sys
from pathlib import Path

MAX_BYTES = 1_000_000

DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm"}
KEY_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
LOCAL_SETTINGS = {"chaosmonkey.toml", "fleet.toml"}

# Checked in every file.
SECRET_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "database URL with a password": re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:[^@\s]+@"),
}

# Checked in settings-style files only (not .py), where NAME=value lines hold real values.
FILLED_IN_SETTING = re.compile(
    r"(?m)^\s*[A-Z0-9_]*(API_KEY|SECRET|TOKEN|PASSWORD)\s*=\s*['\"]?[^\s'\"#]{8,}"
)


def problems_with(path: Path) -> list[str]:
    """Return the reasons this file must not be committed (empty if it is fine)."""
    found: list[str] = []
    parts = path.as_posix().split("/")

    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        found.append("is an .env file, which holds secrets")
    if path.name in LOCAL_SETTINGS:
        found.append("is a local settings file; commit the .example version instead")
    if path.suffix.lower() in KEY_SUFFIXES:
        found.append("is a key or certificate file")
    if parts[0] == "data":
        found.append("is under data/, which holds the local database and simulator state")
    if path.suffix.lower() in DATABASE_SUFFIXES:
        found.append("is a database file")

    if path.is_file():
        size = path.stat().st_size
        if size > MAX_BYTES:
            found.append(f"is {size:,} bytes, over the {MAX_BYTES:,} byte limit")
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    found.append(f"contains what looks like a {name}")
            if path.suffix != ".py" and FILLED_IN_SETTING.search(text):
                found.append("contains what looks like a filled-in key, token or password")
    return found


def main(paths: list[str]) -> int:
    failed = False
    for raw in paths:
        for reason in problems_with(Path(raw)):
            print(f"BLOCKED {raw}: {reason}")
            failed = True
    if failed:
        print("Commit stopped. Remove these files from the commit (git restore --staged <file>).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
