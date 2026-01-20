
from __future__ import annotations
from typing import Any, AsyncIterator, Callable, Coroutine, List, Dict

from homeassistant.core import HomeAssistant, callback
from homeassistant.components.backup import (
    BackupAgent,
    BackupNotFound,
    AgentBackup,
)
from homeassistant.components.backup.util import suggested_filename_from_name_date
from homeassistant.components.backup.const import DATA_MANAGER

from .const import (
    DOMAIN,
    DATA_CLIENT,
    DATA_BACKUP_AGENT_LISTENERS,
    AGENT_NAME,
    ID_TAG,
    VER_TAG,
    PROT_TAG,
)
from .client import KDriveClient

CHUNK_LIMIT = 900 * 1024 * 1024  # 900 MiB

async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    if DOMAIN not in hass.data or DATA_CLIENT not in hass.data[DOMAIN]:
        return []
    client: KDriveClient = hass.data[DOMAIN][DATA_CLIENT]
    return [KDriveBackupAgent(hass=hass, client=client)]

@callback
def async_register_backup_agents_listener(hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any):
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)
    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)

    return remove_listener

# Helpers

def make_filename(backup: AgentBackup) -> str:
    base = suggested_filename_from_name_date(backup.name, backup.date)
    ver = getattr(backup, "homeassistant_version", "") or "unknown"
    prot = "true" if getattr(backup, "protected", False) else "false"
    stem = base[:-4] if base.endswith('.tar') else base
    return f"{stem}{ID_TAG}{backup.backup_id}{VER_TAG}{ver}{PROT_TAG}{prot}.tar"


def try_parse_filename(name: str) -> dict | None:
    # Forme attendue (ordre stable): <suggested>__id-<id>__ver-<ver>__prot-<true|false>.tar
    if not name.endswith('.tar'):
        return None
    stem = name[:-4]
    if ID_TAG not in stem:
        return None
    parts = stem.split('__')
    # parts[0] = suggested prefix
    meta = {"name_hint": parts[0], "backup_id": None, "version": None, "protected": None}
    for p in parts[1:]:
        if p.startswith(ID_TAG.strip('_')):  # 'id-'
            meta["backup_id"] = p[len('id-'):]
        elif p.startswith(VER_TAG.strip('_')):  # 'ver-'
            meta["version"] = p[len('ver-'):]
        elif p.startswith(PROT_TAG.strip('_')):  # 'prot-'
            prot_val = p[len('prot-'):].lower()
            meta["protected"] = prot_val == 'true'
    if not meta["backup_id"]:
        return None
    return meta


def _get_ha_retention_count(hass: HomeAssistant) -> int | None:
    try:
        manager = hass.data.get(DATA_MANAGER)
        if not manager:
            return None
        cfg = getattr(manager, 'config', None)
        data = getattr(cfg, 'data', None) if cfg else None
        candidates = []
        if isinstance(data, dict):
            if isinstance(data.get('retention'), dict):
                candidates.append(data['retention'].get('count'))
            if isinstance(data.get('automatic'), dict):
                auto = data['automatic']
                if isinstance(auto.get('retention'), dict):
                    candidates.append(auto['retention'].get('count'))
            candidates.append(data.get('retention_count'))
        if hasattr(cfg, 'retention_count'):
            candidates.append(getattr(cfg, 'retention_count'))
        for v in candidates:
            if v is not None:
                try:
                    v_int = int(v)
                    if v_int > 0:
                        return v_int
                except (TypeError, ValueError):
                    continue
    except Exception:
        return None
    return None


def _get_current_ha_version(hass: HomeAssistant) -> str:
    if hasattr(hass.config, "version") and hass.config.version:
        return str(hass.config.version)
    try:
        hassio_data = hass.data.get("hassio") or {}
        core_ver = hassio_data.get("core_version")
        if core_ver:
            return str(core_ver)
    except Exception:
        pass
    return ""

class KDriveBackupAgent(BackupAgent):
    domain = DOMAIN
    name = AGENT_NAME
    unique_id = "infomaniak_kdrive_default"

    def __init__(self, hass: HomeAssistant, client: KDriveClient) -> None:
        self._hass = hass
        self._client = client

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        **kwargs: Any,
    ) -> None:
        filename = make_filename(backup)
        size_hint = getattr(backup, "size", None)
        if(size_hint<CHUNK_LIMIT):
          await self._client.upload_stream_to_folder(filename=filename, open_stream=open_stream, size_hint=size_hint)
        else: 
          await self._client.upload_stream_to_folder_by_chunk(filename=filename, open_stream=open_stream, size_hint=size_hint)
        
        retention = _get_ha_retention_count(self._hass)
        if retention is not None and retention > 0:
            await self._enforce_retention(retention)

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        items = await self._client.list_folder_files()
        backups: list[AgentBackup] = []
        default_version = _get_current_ha_version(self._hass)
        for it in items:
            name = it.get("name", "")
            meta = try_parse_filename(name)
            if not meta:
                continue
            size_val = it.get("size")
            if size_val is None:
                try:
                    size_val = await self._client.get_file_size(it["id"])
                except Exception:
                    size_val = 0
            backups.append(
                AgentBackup(
                    backup_id=meta["backup_id"],
                    name=meta["name_hint"],
                    date=None,
                    folders=[],
                    homeassistant_included=True,
                    homeassistant_version=meta.get("version") or default_version,
                    protected=bool(meta.get("protected", False)),
                    size=int(size_val or 0),
                    database_included=True,
                    addons=[],
                    extra_metadata={"source": "kdrive"},
                )
            )
        return backups

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        for b in await self.async_list_backups():
            if b.backup_id == backup_id:
                return b
        raise BackupNotFound(f"Backup not found: {backup_id}")

    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        items = await self._client.list_folder_files()
        # Le nom contient toujours __id-<id>
        match = next((it for it in items if f"__id-{backup_id}" in it.get("name", "") and it.get("name", "").endswith('.tar')), None)
        if not match:
            raise BackupNotFound(f"Archive not found for {backup_id}")
        return await self._client.download_file_stream(match["id"])

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        items = await self._client.list_folder_files()
        match = next((it for it in items if f"__id-{backup_id}" in it.get("name", "") and it.get("name", "").endswith('.tar')), None)
        if not match:
            raise BackupNotFound(f"No remote file for {backup_id}")
        await self._client.delete_file(match["id"])
        await self._client.delete_file_from_trash(match["id"])

    async def _enforce_retention(self, retention_count: int) -> None:
        items = await self._client.list_folder_files()
        candidates: list[dict] = []
        for it in items:
            if try_parse_filename(it.get("name", "")):
                candidates.append(it)
        if len(candidates) <= retention_count:
            return
        candidates.sort(key=lambda it: it.get("name", ""))
        surplus = candidates[:-retention_count]
        for it in surplus:
            try:
                await self._client.delete_file(it["id"])
            except Exception:
                continue
