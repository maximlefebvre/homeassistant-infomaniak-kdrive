
from __future__ import annotations
import logging
import voluptuous as vol
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant import config_entries

from .const import DOMAIN, CONF_DRIVE_ID, CONF_FOLDER_ID, CONF_FOLDER_URL, SCOPES, parse_kdrive_folder_url

_LOGGER = logging.getLogger(__name__)

class OAuth2FlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._drive_id = None
        self._folder_id = None

    @property
    def extra_authorize_data(self) -> dict:
        return {"scope": SCOPES}

    async def async_oauth_create_entry(self, data: dict):
        return self.async_create_entry(title=f"kDrive {self._drive_id}:{self._folder_id}", data={CONF_DRIVE_ID: self._drive_id, CONF_FOLDER_ID: self._folder_id})

    async def async_step_user(self, user_input=None):
        description_placeholders = {"url_help": "Collez l'URL complète du dossier kDrive (ex: https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890)"}
        if user_input is not None:
            try:
                self._drive_id, self._folder_id = parse_kdrive_folder_url(user_input[CONF_FOLDER_URL])
            except Exception:
                return self.async_show_form(step_id="user", data_schema=vol.Schema({vol.Required(CONF_FOLDER_URL): str}), errors={"base": "invalid_folder_url"}, description_placeholders=description_placeholders)
            return await self.async_step_pick_implementation()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({vol.Required(CONF_FOLDER_URL): str}), description_placeholders=description_placeholders)
