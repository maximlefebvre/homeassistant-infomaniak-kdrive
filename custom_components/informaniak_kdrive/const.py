
from __future__ import annotations
import re

DOMAIN = "informaniak_kdrive"

CONF_TOKEN = "token"
CONF_DRIVE_ID = "drive_id"
CONF_FOLDER_ID = "folder_id"
CONF_FOLDER_URL = "folder_url"

DATA_CLIENT = "client"
DATA_BACKUP_AGENT_LISTENERS = "backup_agent_listeners"

AGENT_NAME = "Informaniak kDrive"

ID_TAG = "__id-"
VER_TAG = "__ver-"
PROT_TAG = "__prot-"

OAUTH2_AUTHORIZE = "https://login.infomaniak.com/authorize"
OAUTH2_TOKEN = "https://login.infomaniak.com/token"
SCOPES = "kdrive:read kdrive:write"

KDRIVE_FOLDER_RE = re.compile(r"/drive/(?P<drive_id>\d+)/files/(?P<folder_id>\d+)(?:/|$)")

def parse_kdrive_folder_url(url: str) -> tuple[int, int]:
    m = KDRIVE_FOLDER_RE.search(url)
    if not m:
        raise ValueError("URL de dossier kDrive non reconnue")
    return int(m.group("drive_id")), int(m.group("folder_id"))
