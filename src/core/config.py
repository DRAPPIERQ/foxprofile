import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    """Read the FOXPROFILE_<key> environment variable."""
    return os.getenv(f"FOXPROFILE_{key}", default)


PROFILES_FILE = _env("PROFILES_FILE", "profiles.json")
DATA_DIR = _env("DATA_DIR", "camoufox_data")
# Videos recorded from a profile's tab, one sub-folder per profile. Kept out of
# DATA_DIR so that exporting a profile does not zip its videos.
RECORDINGS_DIR = _env("RECORDINGS_DIR", "recordings")


def _non_negative(key: str, default: float) -> float:
    """FOXPROFILE_<key> as a number >= 0; an unreadable value falls back to `default`."""
    try:
        return max(0.0, float(_env(key, str(default))))
    except ValueError:
        return default


# A recording stops by itself after this many minutes, or when free disk space
# drops below this many MB (0 turns either limit off).
RECORDING_MAX_MINUTES = _non_negative("RECORDING_MAX_MINUTES", 60)
RECORDING_MIN_FREE_MB = _non_negative("RECORDING_MIN_FREE_MB", 1024)
LOG_DIR = _env("LOG_DIR", "logs")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
PROXY_CHECK_TIMEOUT = int(_env("PROXY_TIMEOUT", "10"))
LANGUAGE = _env("LANG", "en")

API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = int(_env("API_PORT", "8000"))

# Seconds the API waits for a launched browser to report ready or failed.
LAUNCH_WAIT_TIMEOUT = int(_env("LAUNCH_TIMEOUT", "90"))

FINGERPRINT_FILE = "fingerprint.json"


def _headless(value: str) -> bool | str:
    """'true'/'false', or 'virtual' (Linux: hidden Xvfb display, more stealthy)."""
    value = value.strip().lower()
    if value == "virtual":
        return "virtual"
    return value in ("1", "true", "yes")


# Browsers open hidden when true; server mode turns this on by default.
HEADLESS = _headless(_env("HEADLESS", "false"))

# Reopen the tabs a profile had open when it was last stopped.
RESTORE_TABS = _env("RESTORE_TABS", "true").strip().lower() in ("1", "true", "yes")

# Shared secret for the REST API, the web panel and the MCP server. Required
# whenever the API listens on anything other than the loopback interface.
API_TOKEN = _env("API_TOKEN", "")
