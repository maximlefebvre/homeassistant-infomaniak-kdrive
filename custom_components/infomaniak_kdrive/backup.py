
from __future__ import annotations
import asyncio
import io
import json
import logging
import re
import tarfile
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Coroutine, List, Dict

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
    """Nom de l'archive: <suggested_filename>__id-<backup_id>.tar

    Toutes les métadonnées vivent dans le sidecar. Le backup_id reste dans le
    nom comme filet de sécurité: si le sidecar est perdu ou illisible, l'archive
    reste identifiable, listable et supprimable.
    """
    base = suggested_filename_from_name_date(backup.name, backup.date)
    stem = base[:-4] if base.endswith('.tar') else base
    return f"{stem}{ID_TAG}{backup.backup_id}.tar"


def sidecar_filename(backup_id: str) -> str:
    return f"{backup_id}{SIDECAR_SUFFIX}"


def try_parse_filename(name: str) -> dict | None:
    """Lit les métadonnées encodées dans le nom d'une archive.

    Tolère les deux formes:
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


# suggested_filename_from_name_date() produit "<nom> <date:%Y-%m-%d %H.%M %S%f>"
# avec les espaces remplacés par des '_'. La date reste donc récupérable.
LEGACY_NAME_DATE_RE = re.compile(
    r"^(?P<name>.+)_(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"_(?P<H>\d{2})\.(?P<M>\d{2})_(?P<S>\d{2})(?P<us>\d{6})$"
)


def recover_name_and_date(name_hint: str, item: dict) -> tuple[str, str]:
    """Reconstruit (nom lisible, date ISO) pour une archive sans sidecar."""
    if m := LEGACY_NAME_DATE_RE.match(name_hint):
        try:
            # Le nom a été généré en heure locale de l'instance.
            when = datetime(
                int(m["y"]), int(m["mo"]), int(m["d"]),
                int(m["H"]), int(m["M"]), int(m["S"]), int(m["us"]),
                tzinfo=dt_util.DEFAULT_TIME_ZONE,
            )
            return m["name"].replace('_', ' '), when.isoformat()
        except ValueError:
            pass
    # Secours: la date de création côté kDrive (~ l'heure de l'upload).
    for key in ("created_at", "added_at", "last_modified_at"):
        raw = item.get(key)
        if raw:
            try:
                return name_hint.replace('_', ' '), dt_util.utc_from_timestamp(int(raw)).isoformat()
            except (TypeError, ValueError):
                continue
    return name_hint.replace('_', ' '), dt_util.utcnow().isoformat()


# Une archive de backup HA contient ./backup.json à sa racine, écrit en premier.
# 256 Kio suffisent très largement pour l'atteindre sans télécharger l'archive.
ARCHIVE_HEAD_SIZE = 256 * 1024


def read_backup_json(head: bytes) -> dict | None:
    """Extrait ./backup.json du début d'une archive de backup HA.

    `head` n'est qu'un préfixe de l'archive: la lecture séquentielle lèvera en
    fin de tampon, ce qui est sans importance une fois backup.json trouvé.
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
    """Construit un AgentBackup depuis backup.json.

    Reproduit homeassistant.components.backup.util.read_backup(), qui ne peut
    pas être réutilisée telle quelle: elle exige un fichier sur disque.
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
        # file_id -> (marqueur de révision, payload) : évite de retélécharger les
        # sidecars à chaque listing (HA appelle async_list_backups souvent).
        self._sidecar_cache: dict[int, tuple[tuple, dict]] = {}
        # file_id -> (backup, source) pour les archives sans sidecar: évite de
        # relire l'en-tête de l'archive à chaque listing.
        self._legacy_cache: dict[int, tuple[AgentBackup, str]] = {}
        self._backfilled: set[str] = set()
        # backups dont on a déjà tenté de régénérer le sidecar dégradé
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
        except Exception:
            _LOGGER.warning("Unreadable backup metadata sidecar: %s", item.get("name"))
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("backup"), dict):
            _LOGGER.warning("Malformed backup metadata sidecar: %s", item.get("name"))
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
            # "archive" = métadonnées réelles lues dans backup.json,
            # "filename" = reconstruites depuis le nom de fichier (dégradées).
            payload["migrated_from"] = migrated
        # kDrive refuse (ou renomme) un fichier existant: on retire l'ancien
        # sidecar avant de réécrire. Cas rare, uniquement lors d'un backfill.
        if replaces:
            self._sidecar_cache.pop(replaces.get("id"), None)
            try:
                await self._client.delete_file(replaces["id"])
            except Exception:
                _LOGGER.debug("Could not remove previous sidecar %s", replaces.get("name"))
        await self._client.upload_bytes(
            filename=sidecar_filename(backup.backup_id),
            data=json.dumps(payload).encode("utf-8"),
        )

    # --- Index ---------------------------------------------------------

    async def _build_index(self) -> dict[str, dict]:
        """backup_id -> {backup, archive_item, sidecar_item, legacy}.

        Deux sources, dans cet ordre de priorité:
          1. les sidecars .metadata.json (source de vérité);
          2. les archives .tar sans sidecar, dont les métadonnées sont relues
             depuis le nom (backups créés avant 0.6.0).
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
        pending_upgrade: list[tuple[str, dict, dict]] = []
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
                # Sidecar orphelin: on ne l'expose pas (backup non restaurable)
                # et on ne le supprime pas non plus, le listing pouvant être partiel.
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
            # Sidecar écrit à partir du seul nom de fichier: ses métadonnées
            # sont dégradées (extra_metadata vide, donc backup automatique non
            # reconnu par HA). On le régénère depuis l'archive.
            if (
                payload.get("migrated_from") == "filename"
                and backup.backup_id not in self._upgrade_attempted
            ):
                pending_upgrade.append((backup.backup_id, archive_item, sidecar_item))

        pending_backfill: list[tuple[dict, AgentBackup, str]] = []
        for it in items:
            meta = try_parse_filename(it.get("name", ""))
            if not meta or meta["backup_id"] in index:
                continue
            backup, source = await self._legacy_backup(it, meta)
            index[backup.backup_id] = {
                "backup": backup,
                "archive_item": it,
                "sidecar_item": None,
                "legacy": True,
            }
            if backup.backup_id not in self._backfilled:
                pending_backfill.append((it, backup, source))

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
        """Lit les vraies métadonnées dans ./backup.json au début de l'archive.

        C'est la seule source qui porte extra_metadata (instance_id +
        with_automatic_settings, dont HA a besoin pour reconnaître ses backups
        automatiques), les dossiers et les add-ons: rien de tout ça n'est
        déductible du nom de fichier.
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
        """Métadonnées d'une archive sans sidecar (créée avant 0.6.0).

        Retourne (backup, source) où source vaut "archive" si les métadonnées
        réelles ont pu être lues dans l'archive, "filename" sinon.
        """
        cached = self._legacy_cache.get(item.get("id"))
        if cached:
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
            # Repli: tout ce que le nom de fichier permet de reconstituer.
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

        self._legacy_cache[item.get("id")] = result
        return result

    # --- Backfill ------------------------------------------------------

    def _schedule_backfill(
        self,
        pending: list[tuple[dict, AgentBackup, str]],
        upgrades: list[tuple[str, dict, dict]] | None = None,
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

    async def _run_upgrade(self, upgrades: list[tuple[str, dict, dict]]) -> None:
        """Régénère les sidecars dont les métadonnées viennent du nom de fichier.

        Ceux-là ont été écrits avant que la lecture de ./backup.json existe:
        leur extra_metadata est vide, donc HA ne reconnaît pas les backups
        automatiques. On les remplace par les métadonnées réelles.
        """
        for backup_id, archive_item, sidecar_item in upgrades:
            # Marqué quoi qu'il arrive: si l'archive est illisible, inutile de
            # réessayer à chaque listing. Un redémarrage relance la tentative.
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

    async def _run_backfill(self, pending: list[tuple[dict, AgentBackup, str]]) -> None:
        """Écrit un sidecar pour les archives qui n'en ont pas encore.

        Best-effort et idempotent: en cas d'échec, le fallback reste en place
        et on retentera au prochain listing.
        """
        for item, backup, source in pending:
            if backup.backup_id in self._backfilled:
                continue
            try:
                await self._write_sidecar(
                    backup,
                    archive_name=item.get("name", ""),
                    archive_file_id=item.get("id"),
                    migrated=source,
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
        # L'archive d'abord: un sidecar sans archive créerait un backup fantôme,
        # alors qu'une archive sans sidecar reste lisible via son nom.
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
        return await self._client.download_file_stream(entry["archive_item"]["id"])

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        entry = (await self._build_index()).get(backup_id)
        if not entry:
            raise BackupNotFound(f"No remote file for {backup_id}")
        await self._delete_entry(entry)

    async def _delete_entry(self, entry: dict) -> None:
        """Supprime l'archive et son sidecar (l'un des deux peut manquer)."""
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
                # Le fichier est déjà hors du dossier: ne pas faire échouer la
                # suppression pour un nettoyage de corbeille incomplet.
                _LOGGER.debug("Could not purge %s from trash", item.get("name"))
        self._backfilled.discard(entry["backup"].backup_id)

    async def _enforce_retention(self, retention_count: int) -> None:
        index = await self._build_index()
        # Tri sur des datetime, pas sur les chaînes ISO: deux backups au même
        # instant mais avec des offsets différents ("+02:00" / "Z") se
        # compareraient dans le mauvais ordre, et on supprimerait le mauvais.
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
