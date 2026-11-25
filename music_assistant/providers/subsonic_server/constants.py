"""Constants for the Subsonic API server."""

from __future__ import annotations

from typing import Final

API_VERSION: Final = "1.16.1"
SERVER_TYPE_MA: Final = "music-assistant"
SERVER_TYPE_NAVIDROME: Final = "navidrome"
# reported as serverVersion when the server presents itself as Navidrome
NAVIDROME_VERSION: Final = "0.64.2 (music-assistant)"
XML_NAMESPACE: Final = "http://subsonic.org/restapi"

CONF_PORT: Final = "port"
CONF_BIND_IP: Final = "bind_ip"
CONF_MUSIC_FOLDERS: Final = "music_folders"
CONF_LOG_REQUESTS: Final = "log_requests"
CONF_SERVER_TYPE: Final = "server_type"
CONF_PASSWORD_PREFIX: Final = "password_"
DEFAULT_PORT: Final = 4040
DEFAULT_BIND_IP: Final = "0.0.0.0"

# the parameters that carry credentials; never logged
SECRET_PARAMS: Final = frozenset({"u", "p", "t", "s", "apiKey"})

# Navidrome's default list of articles ignored when indexing artists
IGNORED_ARTICLES: Final = "The El La Los Las Le Les Os As O A"

# failed logins from one address before it is locked out, and for how long
MAX_FAILED_LOGINS: Final = 10
FAILED_LOGIN_WINDOW: Final = 300
LOCKOUT_SECONDS: Final = 300

# list sizes (Subsonic defaults and caps)
DEFAULT_LIST_SIZE: Final = 10
MAX_LIST_SIZE: Final = 500
DEFAULT_SEARCH_COUNT: Final = 20

# rebuild the library index this long after the last library change
INDEX_REBUILD_DELAY: Final = 3.0

DB_FILE: Final = "subsonic_server.db"

# Subsonic error codes
ERR_GENERIC: Final = 0
ERR_MISSING_PARAM: Final = 10
ERR_WRONG_CREDENTIALS: Final = 40
ERR_AUTH_NOT_SUPPORTED: Final = 42
ERR_NOT_AUTHORIZED: Final = 50
ERR_NOT_FOUND: Final = 70

CONTENT_TYPES: Final = {
    "aac": "audio/aac",
    "aif": "audio/aiff",
    "aiff": "audio/aiff",
    "alac": "audio/mp4",
    "ape": "audio/x-ape",
    "dsf": "audio/dsd",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "m4b": "audio/mp4",
    "mka": "audio/x-matroska",
    "mp2": "audio/mpeg",
    "mp3": "audio/mpeg",
    "mp4": "audio/mp4",
    "mpc": "audio/x-musepack",
    "oga": "audio/ogg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/x-wav",
    "wma": "audio/x-ms-wma",
    "wv": "audio/x-wavpack",
}
