import subprocess
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


def main() -> int:
    paths = (
        subprocess.check_output(  # nosec B603,B607 - fixed Git command, no user arguments
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        )
        .decode()
        .split("\0")
    )
    secrets = SecretsCollection()
    with default_settings():
        for name in paths:
            path = Path(name)
            if path.is_file() and path.name not in {"uv.lock", "requirements.txt"}:
                secrets.scan_file(name)
    findings = secrets.json()
    for filename, entries in findings.items():
        for entry in entries:
            print(f"Potential secret: {filename}:{entry['line_number']} ({entry['type']})")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
