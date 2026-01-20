
from __future__ import annotations
from typing import AsyncIterator, Dict, List, Optional
import os
import tempfile

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

class KDriveClient:
    def __init__(self, hass: HomeAssistant, token: Optional[str], drive_id: int, folder_id: int):
        self._hass = hass
        self._token = token
        self._drive_id = drive_id
        self._folder_id = folder_id
        self._session = async_get_clientsession(hass)
        self._base_v3 = f"https://api.infomaniak.com/3/drive/{drive_id}"
        self._base_v2 = f"https://api.infomaniak.com/2/drive/{drive_id}"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def list_folder_files(self) -> List[Dict]:
        url = f"{self._base_v3}/files/{self._folder_id}/files"
        async with self._session.get(url, headers=self._headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            items = data.get("data", [])
            return [it for it in items if it.get("type") == "file"]

    async def get_file_size(self, file_id: int) -> int:
        url = f"{self._base_v3}/files/{file_id}/download"
        try:
            async with self._session.head(url, headers=self._headers) as resp:
                if resp.status < 400:
                    cl = resp.headers.get('Content-Length')
                    if cl is not None:
                        try:
                            return int(cl)
                        except ValueError:
                            pass
        except Exception:
            pass
        try:
            async with self._session.get(url, headers=self._headers) as resp:
                resp.raise_for_status()
                cl = resp.headers.get('Content-Length')
                if cl is not None:
                    try:
                        return int(cl)
                    except ValueError:
                        pass
        except Exception:
            pass
        return 0

    async def delete_file(self, file_id: int) -> None:
        url = f"{self._base_v2}/files/{file_id}"
        async with self._session.delete(url, headers=self._headers) as resp:
            resp.raise_for_status()

    async def delete_file_from_trash(self, file_id: int) -> None:
        url = f"{self._base_v2}/trash/{file_id}"
        async with self._session.delete(url, headers=self._headers) as resp:
            resp.raise_for_status()

    async def download_file_stream(self, file_id: int) -> AsyncIterator[bytes]:
        url = f"{self._base_v3}/files/{file_id}/download"
        resp = await self._session.get(url, headers=self._headers)
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            yield chunk

    async def upload_stream_to_folder(self, *, filename: str, open_stream, size_hint: Optional[int] = None) -> None:
        if size_hint is None:
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="ha-kdrive-", suffix=".tar")
            os.close(tmp_fd)
            try:
                async def _write_tmp():
                    async for chunk in await open_stream():
                        with open(tmp_path, "ab") as f:
                            f.write(chunk)
                await _write_tmp()
                total_size = os.path.getsize(tmp_path)
                url = f"{self._base_v3}/upload?total_size={total_size}&directory_id={self._folder_id}&file_name={filename}"
                with open(tmp_path, "rb") as f:
                    async with self._session.post(url, headers=self._headers, data=f) as resp:
                        resp.raise_for_status()
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        else:
            url = f"{self._base_v3}/upload?total_size={size_hint}&directory_id={self._folder_id}&file_name={filename}"
            async with self._session.post(url, headers=self._headers, data=await open_stream()) as resp:
                resp.raise_for_status()
