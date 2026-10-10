"""Fail if local configuration or secrets would be committed.

Checks every file Git tracks (or every file, outside Git):
  * no secret-bearing files (.env, keys, certificates, OAuth state, tool config)
  * no SmartThings credentials in text: a personal access token or OAuth token /
    client secret looks like a UUID, so flag UUIDs next to token-ish words.
    Test code uses obviously fake values (pat-0123456789, secret-xyz, ...).

Usage:  python scripts/check_repo.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_NAMES = [r"(^|/)\.env(\..*)?$", r"\.pem$", r"\.key$", r"\.p8$", r"(^|/)\.claude(/|\.json$)",
                   r"oauth_state.*\.json$", r"(^|/)secrets?\.", r"\.c4z$"]
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
SECRET_PATTERNS = [
    rf"(?i)bearer\s+{UUID}",
    rf"(?i)(token|secret|client_?id|authorization|password)[\"'\s:=]{{1,6}}{UUID}",
    rf"(?i)ST_TOKEN\s*=\s*[\"']?{UUID}",
]
SKIP_DIRS = ("tests/fixtures/",)          # public Home Assistant fixtures (recorded device data, no credentials)
TEXT_SUFFIXES = {".py", ".lua", ".xml", ".html", ".md", ".txt", ".yml", ".yaml", ".json", ".cfg", ".toml", ""}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, check=True).stdout
        return [p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                if p.is_file() and ".git/" not in p.as_posix() and "/dist/" not in p.as_posix()]


def main():
    problems = []
    for rel in tracked_files():
        for pat in FORBIDDEN_NAMES:
            if re.search(pat, rel):
                problems.append(f"{rel}: this kind of file must not be committed")
        path = ROOT / rel
        if rel.startswith(SKIP_DIRS) or path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: looks like a SmartThings credential")
    if problems:
        print("check_repo FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("check_repo: no secrets or local configuration found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
