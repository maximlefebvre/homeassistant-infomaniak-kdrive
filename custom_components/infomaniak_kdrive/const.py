
from __future__ import annotations
import re

DOMAIN = "infomaniak_kdrive"

CONF_TOKEN = "token"
CONF_DRIVE_ID = "drive_id"
CONF_FOLDER_ID = "folder_id"
CONF_FOLDER_URL = "folder_url"

DATA_CLIENT = "client"
DATA_BACKUP_AGENT_LISTENERS = "backup_agent_listeners"

AGENT_NAME = "Infomaniak kDrive"

# Markers encoded in the archive filename.
# ID_TAG is always written (a safety net in case the sidecar is lost).
# VER_TAG / PROT_TAG are no longer written since 0.6.0, but are still read so
# that archives created by earlier versions keep working.
ID_TAG = "__id-"
VER_TAG = "__ver-"
PROT_TAG = "__prot-"

# Metadata sidecar: one <backup_id>.metadata.json file per archive.
SIDECAR_SUFFIX = ".metadata.json"
METADATA_VERSION = 1

KDRIVE_FOLDER_RE = re.compile(r"/drive/(?P<drive_id>\d+)/files/(?P<folder_id>\d+)(?:/|$)")

def parse_kdrive_folder_url(url: str) -> tuple[int, int]:
    m = KDRIVE_FOLDER_RE.search(url)
    if not m:
        raise ValueError("Unrecognised kDrive folder URL")
    return int(m.group("drive_id")), int(m.group("folder_id"))
