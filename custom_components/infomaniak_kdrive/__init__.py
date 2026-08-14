
from __future__ import annotations
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DATA_CLIENT, CONF_TOKEN, CONF_DRIVE_ID, CONF_FOLDER_ID
from .client import KDriveClient

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    token = entry.data.get(CONF_TOKEN)
    drive_id = entry.data[CONF_DRIVE_ID]
    folder_id = entry.data[CONF_FOLDER_ID]

    client = KDriveClient(
        hass=hass,
        token=token,
        drive_id=drive_id,
        folder_id=folder_id,
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_CLIENT] = client

    def _notify_backup_listeners() -> None:
        for listener in hass.data.get("backup_agent_listeners", []):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error notifying backup listeners")

    entry.async_on_unload(entry.async_on_state_change(_notify_backup_listeners))
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(DATA_CLIENT, None)
    return True
