
from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN, CONF_TOKEN, CONF_FOLDER_URL, CONF_DRIVE_ID, CONF_FOLDER_ID, parse_kdrive_folder_url

class InforaniakKDriveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 5

    async def async_step_user(self, user_input=None):
        schema = vol.Schema({
            vol.Required(CONF_TOKEN): str,
            vol.Required(CONF_FOLDER_URL): str,
        })
        description_placeholders = {
            "token_help": "Enter your kDrive API token (generated in your Infomaniak account)",
            "url_help": "Paste the full kDrive folder URL (e.g. https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890)",
        }
        if user_input is not None:
            try:
                drive_id, folder_id = parse_kdrive_folder_url(user_input[CONF_FOLDER_URL])
            except Exception:
                return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "invalid_folder_url"}, description_placeholders=description_placeholders)
            data = {CONF_TOKEN: user_input[CONF_TOKEN], CONF_DRIVE_ID: drive_id, CONF_FOLDER_ID: folder_id}
            uid = f"{drive_id}:{folder_id}"
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"kDrive {uid}", data=data)
        return self.async_show_form(step_id="user", data_schema=schema, description_placeholders=description_placeholders)
