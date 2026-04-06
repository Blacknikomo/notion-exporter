import os
import sys
from pathlib import Path


def parse_dotenv(path: Path) -> dict:
    result = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    except OSError:
        pass
    return result


def load_config() -> dict:
    """Load NOTION_TOKEN and OBSIDIAN_VAULT_PATH from env or .env file.

    Returns a dict with keys: token (str), vault_path (Path).
    Exits with an error message if required variables are missing.
    """
    env = {}
    dotenv_path = Path(".env")
    if dotenv_path.exists():
        env = parse_dotenv(dotenv_path)

    # os.environ takes precedence over .env
    token = os.environ.get("NOTION_TOKEN") or env.get("NOTION_TOKEN")
    vault_path_str = os.environ.get("OBSIDIAN_VAULT_PATH") or env.get("OBSIDIAN_VAULT_PATH")

    missing = [name for name, val in [("NOTION_TOKEN", token), ("OBSIDIAN_VAULT_PATH", vault_path_str)] if not val]
    if missing:
        print(f"Error: missing required variable(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them in a .env file or as environment variables.", file=sys.stderr)
        sys.exit(1)

    vault_path = Path(vault_path_str).expanduser()
    if not vault_path.exists():
        print(f"Error: OBSIDIAN_VAULT_PATH does not exist: {vault_path}", file=sys.stderr)
        sys.exit(1)

    return {"token": token, "vault_path": vault_path}
