
from __future__ import annotations
import asyncio
import io
import json
import logging
import re
import tarfile
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Coroutine

from homeassistant.core import HomeAssistant, callback
from homeassistant.components.backup import (
    BackupAgent,
    BackupNotFound,
    AgentBackup,
    AddonInfo,
    Folder,
)
from homeassistant.components.backup.util import suggested_filename_from_name_date
from homeassistant.components.backup.const import DATA_MANAGER
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DATA_CLIENT,
    DATA_BACKUP_AGENT_LISTENERS,
    AGENT_NAME,
    ID_TAG,
    VER_TAG,
    PROT_TAG,
    SIDECAR_SUFFIX,
    METADATA_VERSION,
)
from .client import KDriveClient

_LOGGER = logging.getLogger(__name__)

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
    """Archive name: <suggested_filename>__id-<backup_id>.tar

    All metadata lives in the sidecar. The backup_id stays in the name as a
    safety net: if the sidecar is lost or unreadable, the archive can still be
    identified, listed and deleted.
    """
    base = suggested_filename_from_name_date(backup.name, backup.date)
    stem = base[:-4] if base.endswith('.tar') else base
    return f"{stem}{ID_TAG}{backup.backup_id}.tar"


def sidecar_filename(archive_name: str) -> str:
    """Sidecar name for an archive: the archive name with .tar swapped out.

    Keeping both names aligned makes the pair obvious when browsing the folder.
    """
    stem = archive_name[:-4] if archive_name.endswith('.tar') else archive_name
    return f"{stem}{SIDECAR_SUFFIX}"


def try_parse_filename(name: str) -> dict | None:
    """Read the metadata encoded in an archive filename.

    Accepts both shapes:
      - 0.6+   : <suggested>__id-<id>.tar
      - <= 0.5 : <suggested>__id-<id>__ver-<ver>__prot-<true|false>.tar
    """
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


def backup_id_from_sidecar_name(name: str) -> str | None:
    """Backup id encoded in a sidecar filename.

    Only used to pair an unreadable sidecar with its backup; when the sidecar
    can be read, its own payload is authoritative.
    """
    if not name.endswith(SIDECAR_SUFFIX):
        return None
    meta = try_parse_filename(f"{name[: -len(SIDECAR_SUFFIX)]}.tar")
    return meta["backup_id"] if meta else None


# suggested_filename_from_name_date() produces "<name> <date:%Y-%m-%d %H.%M %S%f>"
# with spaces replaced by '_', so the date remains recoverable.
LEGACY_NAME_DATE_RE = re.compile(
    r"^(?P<name>.+)_(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"_(?P<H>\d{2})\.(?P<M>\d{2})_(?P<S>\d{2})(?P<us>\d{6})$"
)


def recover_name_and_date(name_hint: str, item: dict) -> tuple[str, str]:
    """Rebuild (readable name, ISO date) for an archive without a sidecar."""
    if m := LEGACY_NAME_DATE_RE.match(name_hint):
        try:
            # The name was generated in the instance's local time.
            when = datetime(
                int(m["y"]), int(m["mo"]), int(m["d"]),
                int(m["H"]), int(m["M"]), int(m["S"]), int(m["us"]),
                tzinfo=dt_util.DEFAULT_TIME_ZONE,
            )
            return m["name"].replace('_', ' '), when.isoformat()
        except ValueError:
            pass
    # Fallback: the kDrive creation date (roughly the upload time).
    for key in ("created_at", "added_at", "last_modified_at"):
        raw = item.get(key)
        if raw:
            try:
                return name_hint.replace('_', ' '), dt_util.utc_from_timestamp(int(raw)).isoformat()
            except (TypeError, ValueError):
                continue
    return name_hint.replace('_', ' '), dt_util.utcnow().isoformat()


# A HA backup archive holds ./backup.json at its root, written first.
# 256 KiB is far more than enough to reach it without downloading the archive.
ARCHIVE_HEAD_SIZE = 256 * 1024

# (backup_id, archive item, sidecar item) of a sidecar to rebuild
UpgradeEntry = tuple[str, dict, dict]


def read_backup_json(head: bytes) -> dict | None:
    """Extract ./backup.json from the beginning of a HA backup archive.

    `head` is only a prefix of the archive, so the sequential read will raise at
    the end of the buffer; that is harmless once backup.json has been found.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(head), mode="r|") as tar:
            for member in tar:
                if member.name.lstrip("./") != "backup.json":
                    continue
                if (data_file := tar.extractfile(member)) is None:
                    return None
                return json.loads(data_file.read())
    except Exception:
        return None
    return None


def agent_backup_from_backup_json(data: dict, size: int) -> AgentBackup:
    """Build an AgentBackup from backup.json.

    Mirrors homeassistant.components.backup.util.read_backup(), which cannot be
    reused as-is because it requires a file on disk.
    """
    homeassistant = data.get("homeassistant") or {}
    homeassistant_included = "version" in homeassistant
    extra_metadata = data.get("extra") or {}
    return AgentBackup(
        addons=[
            AddonInfo(name=a.get("name"), slug=a["slug"], version=a.get("version"))
            for a in data.get("addons", [])
        ],
        backup_id=data["slug"],
        database_included=(
            not homeassistant.get("exclude_database", False) if homeassistant_included else False
        ),
        date=extra_metadata.get("supervisor.backup_request_date", data["date"]),
        extra_metadata=extra_metadata,
        folders=[
            Folder(f) for f in data.get("folders", []) if f != "homeassistant"
        ],
        homeassistant_included=homeassistant_included,
        homeassistant_version=homeassistant.get("version") if homeassistant_included else None,
        name=data["name"],
        protected=bool(data.get("protected", False)),
        size=size,
    )


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
        # file_id -> (revision marker, payload): avoids re-downloading the
        # sidecars on every listing (HA calls async_list_backups often).
        # A None payload means the read was already tried and failed.
        self._sidecar_cache: dict[int, tuple[tuple, dict | None]] = {}
        # file_id -> (backup, source) for archives without a sidecar: avoids
        # re-reading the archive header on every listing.
        self._legacy_cache: dict[int, tuple[AgentBackup, str]] = {}
        self._backfilled: set[str] = set()
        # backups whose degraded sidecar we already tried to rebuild
        self._upgrade_attempted: set[str] = set()
        self._backfill_task: asyncio.Task | None = None

    # --- Sidecar I/O ---------------------------------------------------

    async def _read_sidecar(self, item: dict) -> dict | None:
        file_id = item.get("id")
        marker = (item.get("last_modified_at"), item.get("size"))
        cached = self._sidecar_cache.get(file_id)
        if cached and cached[0] == marker:
            return cached[1]
        try:
            raw = await self._client.download_file_bytes(file_id)
            payload = json.loads(raw)
        except Exception as err:
            # The failure is remembered: without this, every listing retries
            # the read, then a backfill, then hits a 409 — an endless loop.
            self._sidecar_cache[file_id] = (marker, None)
            _LOGGER.warning(
                "Unreadable backup metadata sidecar %s (%s bytes): %s",
                item.get("name"), item.get("size"), err, exc_info=True,
            )
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("backup"), dict):
            self._sidecar_cache[file_id] = (marker, None)
            _LOGGER.warning(
                "Malformed backup metadata sidecar %s: got keys %s",
                item.get("name"),
                list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__,
            )
            return None
        self._sidecar_cache[file_id] = (marker, payload)
        return payload

    async def _write_sidecar(
        self,
        backup: AgentBackup,
        *,
        archive_name: str,
        archive_file_id: int | None,
        migrated: str | None = None,
        replaces: dict | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "metadata_version": METADATA_VERSION,
            "backup_id": backup.backup_id,
            "archive_name": archive_name,
            "archive_file_id": archive_file_id,
            "backup": backup.as_dict(),
        }
        if migrated:
            # "archive" = real metadata read from backup.json,
            # "filename" = rebuilt from the filename (degraded).
            payload["migrated_from"] = migrated
        # kDrive answers 409 when the name is taken, so the previous sidecar
        # must be removed before rewriting or the write fails forever.
        if replaces:
            self._sidecar_cache.pop(replaces.get("id"), None)
            await self._client.delete_file(replaces["id"])
            try:
                await self._client.delete_file_from_trash(replaces["id"])
            except Exception:
                _LOGGER.debug("Could not purge previous sidecar from trash")
        await self._client.upload_bytes(
            filename=sidecar_filename(archive_name),
            data=json.dumps(payload).encode("utf-8"),
        )

    # --- Index ---------------------------------------------------------

    async def _build_index(self) -> dict[str, dict]:
        """backup_id -> {backup, archive_item, sidecar_item, legacy}.

        Two sources, in order of priority:
          1. the .metadata.json sidecars (the source of truth);
          2. .tar archives without a sidecar, whose metadata is recovered from
             the archive itself or from its name (backups made before 0.6.0).
        """
        items = await self._client.list_folder_files()
        by_name = {it.get("name", ""): it for it in items}
        by_id = {it.get("id"): it for it in items}
        sidecar_items = [it for it in items if it.get("name", "").endswith(SIDECAR_SUFFIX)]
        _LOGGER.debug(
            "kDrive folder listing: %d files, %d archives, %d metadata sidecars",
            len(items),
            sum(1 for it in items if it.get("name", "").endswith(".tar")),
            len(sidecar_items),
        )

        sem = asyncio.Semaphore(5)

        async def _load(it: dict) -> tuple[dict, dict | None]:
            async with sem:
                return it, await self._read_sidecar(it)

        index: dict[str, dict] = {}
        pending_upgrade: list[UpgradeEntry] = []
        for sidecar_item, payload in await asyncio.gather(*(_load(it) for it in sidecar_items)):
            if not payload:
                continue
            try:
                backup = AgentBackup.from_dict(payload["backup"])
            except Exception:
                _LOGGER.warning("Ignoring unusable sidecar %s", sidecar_item.get("name"), exc_info=True)
                continue
            archive_item = by_id.get(payload.get("archive_file_id"))
            if archive_item is None:
                archive_item = by_name.get(payload.get("archive_name") or "")
            if archive_item is None:
                archive_item = self._find_archive_by_id(items, backup.backup_id)
            if archive_item is None:
                # Orphan sidecar: not exposed (the backup cannot be restored)
                # and not deleted either, since the listing may be partial.
                _LOGGER.warning(
                    "Backup metadata found without its archive, ignoring: %s", sidecar_item.get("name")
                )
                continue
            index[backup.backup_id] = {
                "backup": backup,
                "archive_item": archive_item,
                "sidecar_item": sidecar_item,
                "legacy": False,
            }
            # Sidecar written from the filename alone: its metadata is
            # degraded (empty extra_metadata, so HA cannot recognise an
            # automatic backup). Rebuild it from the archive.
            if (
                payload.get("migrated_from") == "filename"
                and backup.backup_id not in self._upgrade_attempted
            ):
                pending_upgrade.append((backup.backup_id, archive_item, sidecar_item))

        # Sidecars indexed by backup id, including the ones we failed to read:
        # we need to know them to replace them instead of hitting a 409.
        sidecar_by_id = {
            bid: it
            for it in sidecar_items
            if (bid := backup_id_from_sidecar_name(it.get("name", "")))
        }

        pending_backfill: list[tuple[dict, AgentBackup, str, dict | None]] = []
        for it in items:
            meta = try_parse_filename(it.get("name", ""))
            if not meta or meta["backup_id"] in index:
                continue
            backup, source = await self._legacy_backup(it, meta)
            existing = sidecar_by_id.get(backup.backup_id)
            index[backup.backup_id] = {
                "backup": backup,
                "archive_item": it,
                "sidecar_item": existing,
                "legacy": True,
            }
            if backup.backup_id not in self._backfilled:
                pending_backfill.append((it, backup, source, existing))

        # Diagnostics: where each backup's metadata came from, and which ones
        # carry HA's automatic-backup flag.
        _LOGGER.debug(
            "Backup index: %s",
            {
                bid: (
                    ("sidecar" if not e["legacy"] else "archive/filename"),
                    "automatic" if e["backup"].extra_metadata.get("with_automatic_settings") is True
                    else "not-automatic",
                )
                for bid, e in index.items()
            },
        )

        if pending_backfill or pending_upgrade:
            self._schedule_backfill(pending_backfill, pending_upgrade)
        return index

    @staticmethod
    def _find_archive_by_id(items: list[dict], backup_id: str) -> dict | None:
        tag = f"{ID_TAG}{backup_id}"
        return next(
            (it for it in items if tag in it.get("name", "") and it.get("name", "").endswith('.tar')),
            None,
        )

    async def _metadata_from_archive(self, item: dict, size: int) -> AgentBackup | None:
        """Read the real metadata from ./backup.json at the start of the archive.

        This is the only source carrying extra_metadata (instance_id and
        with_automatic_settings, which HA needs to recognise its own automatic
        backups), the folders and the add-ons: none of that can be derived from
        the filename.
        """
        try:
            head = await self._client.download_file_head(item["id"], ARCHIVE_HEAD_SIZE)
            if (data := read_backup_json(head)) is None:
                return None
            return agent_backup_from_backup_json(data, size)
        except Exception:
            _LOGGER.debug("Could not read backup.json from %s", item.get("name"), exc_info=True)
            return None

    async def _legacy_backup(self, item: dict, meta: dict) -> tuple[AgentBackup, str]:
        """Metadata for an archive without a sidecar (created before 0.6.0).

        Returns (backup, source), where source is "archive" when the real
        metadata could be read from the archive, and "filename" otherwise.
        """
        file_id = item.get("id")
        if file_id is not None and (cached := self._legacy_cache.get(file_id)):
            return cached

        size_val = item.get("size")
        if size_val is None:
            try:
                size_val = await self._client.get_file_size(item["id"])
            except Exception:
                size_val = 0
        size_val = int(size_val or 0)

        result: tuple[AgentBackup, str] | None = None
        if (backup := await self._metadata_from_archive(item, size_val)) is not None:
            if backup.backup_id != meta["backup_id"]:
                _LOGGER.debug(
                    "Backup id in %s differs from the one in backup.json (%s vs %s)",
                    item.get("name"), meta["backup_id"], backup.backup_id,
                )
            result = (backup, "archive")

        if result is None:
            # Fallback: whatever the filename allows us to reconstruct.
            name, date = recover_name_and_date(meta["name_hint"], item)
            result = (
                AgentBackup(
                    backup_id=meta["backup_id"],
                    name=name,
                    date=date,
                    folders=[],
                    homeassistant_included=True,
                    homeassistant_version=meta.get("version") or _get_current_ha_version(self._hass),
                    protected=bool(meta.get("protected")),
                    size=size_val,
                    database_included=True,
                    addons=[],
                    extra_metadata={},
                ),
                "filename",
            )

        if file_id is not None:
            self._legacy_cache[file_id] = result
        return result

    # --- Backfill ------------------------------------------------------

    def _schedule_backfill(
        self,
        pending: list[tuple[dict, AgentBackup, str, dict | None]],
        upgrades: list[UpgradeEntry] | None = None,
    ) -> None:
        if self._backfill_task and not self._backfill_task.done():
            return

        async def _run() -> None:
            if pending:
                await self._run_backfill(pending)
            if upgrades:
                await self._run_upgrade(upgrades)

        self._backfill_task = self._hass.async_create_background_task(
            _run(), "infomaniak_kdrive_metadata_backfill"
        )

    async def _run_upgrade(self, upgrades: list[UpgradeEntry]) -> None:
        """Rebuild sidecars whose metadata came from the filename.

        Those were written when the archive could not be read: their
        extra_metadata is empty, so HA does not recognise automatic backups.
        Replace them with the real metadata.
        """
        for backup_id, archive_item, sidecar_item in upgrades:
            # Marked either way: if the archive is unreadable there is no
            # point retrying on every listing. A restart retries it.
            self._upgrade_attempted.add(backup_id)
            size = int(archive_item.get("size") or 0)
            backup = await self._metadata_from_archive(archive_item, size)
            if backup is None:
                _LOGGER.debug(
                    "Could not upgrade degraded sidecar %s: archive unreadable",
                    sidecar_item.get("name"),
                )
                continue
            try:
                await self._write_sidecar(
                    backup,
                    archive_name=archive_item.get("name", ""),
                    archive_file_id=archive_item.get("id"),
                    migrated="archive",
                    replaces=sidecar_item,
                )
            except Exception:
                _LOGGER.warning(
                    "Could not rewrite degraded sidecar for backup %s",
                    backup.backup_id, exc_info=True,
                )
                continue
            _LOGGER.info(
                "Rebuilt metadata of backup %s from its archive (was reconstructed "
                "from the filename, which loses the automatic-backup flag)",
                backup.backup_id,
            )

    async def _run_backfill(self, pending: list[tuple[dict, AgentBackup, str, dict | None]]) -> None:
        """Write a sidecar for archives that do not have one yet.

        Best-effort and idempotent: on failure the fallback stays in place and
        we retry on the next listing.
        """
        for item, backup, source, existing in pending:
            if backup.backup_id in self._backfilled:
                continue
            try:
                await self._write_sidecar(
                    backup,
                    archive_name=item.get("name", ""),
                    archive_file_id=item.get("id"),
                    migrated=source,
                    replaces=existing,
                )
            except Exception:
                _LOGGER.warning(
                    "Could not write metadata sidecar for backup %s", backup.backup_id, exc_info=True
                )
                continue
            self._backfilled.add(backup.backup_id)
            _LOGGER.info(
                "Migrated metadata of backup %s to a sidecar file (read from %s)",
                backup.backup_id, source,
            )

    # --- BackupAgent ---------------------------------------------------

    async def async_upload_backup(self, *, open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]], backup: AgentBackup, **kwargs: Any) -> None:
        filename = make_filename(backup)
        size_hint = getattr(backup, "size", None)
        # Archive first: a sidecar without an archive would create a phantom
        # backup, whereas an archive without a sidecar stays readable.
        file_id = await self._client.upload_stream_to_folder(
            filename=filename, open_stream=open_stream, size_hint=size_hint
        )
        try:
            await self._write_sidecar(backup, archive_name=filename, archive_file_id=file_id)
        except Exception:
            _LOGGER.exception(
                "Backup %s uploaded but its metadata sidecar could not be written", backup.backup_id
            )
        retention = _get_ha_retention_count(self._hass)
        if retention is not None and retention > 0:
            await self._enforce_retention(retention)

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        index = await self._build_index()
        return [entry["backup"] for entry in index.values()]

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        entry = (await self._build_index()).get(backup_id)
        if not entry:
            raise BackupNotFound(f"Backup not found: {backup_id}")
        return entry["backup"]

    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        entry = (await self._build_index()).get(backup_id)
        if not entry:
            raise BackupNotFound(f"Archive not found for {backup_id}")
        # No await: download_file_stream is an async generator, and HA expects
        # the iterator itself as the return value.
        return self._client.download_file_stream(entry["archive_item"]["id"])

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        entry = (await self._build_index()).get(backup_id)
        if not entry:
            raise BackupNotFound(f"No remote file for {backup_id}")
        await self._delete_entry(entry)

    async def _delete_entry(self, entry: dict) -> None:
        """Delete the archive and its sidecar (either one may be missing)."""
        for key in ("archive_item", "sidecar_item"):
            item = entry.get(key)
            if not item:
                continue
            await self._client.delete_file(item["id"])
            self._sidecar_cache.pop(item["id"], None)
            self._legacy_cache.pop(item["id"], None)
            try:
                await self._client.delete_file_from_trash(item["id"])
            except Exception:
                # The file is already out of the folder: do not fail the
                # deletion over an incomplete trash cleanup.
                _LOGGER.debug("Could not purge %s from trash", item.get("name"))
        self._backfilled.discard(entry["backup"].backup_id)

    async def _enforce_retention(self, retention_count: int) -> None:
        index = await self._build_index()
        # Sort on datetimes, not on ISO strings: two backups at the same
        # instant but with different offsets ("+02:00" / "Z") would compare in
        # the wrong order, and we would delete the wrong one.
        def _sort_key(entry: dict):
            parsed = dt_util.parse_datetime(entry["backup"].date or "")
            return (parsed is not None, parsed or dt_util.utc_from_timestamp(0))

        entries = sorted(index.values(), key=_sort_key)
        if len(entries) <= retention_count:
            return
        for entry in entries[:-retention_count]:
            try:
                await self._delete_entry(entry)
            except Exception:
                _LOGGER.warning(
                    "Could not delete backup %s while enforcing retention",
                    entry["backup"].backup_id,
                    exc_info=True,
                )
                continue
