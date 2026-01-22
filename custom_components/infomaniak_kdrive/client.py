
from __future__ import annotations
import math
from typing import AsyncIterator, Dict, List, Optional
import os
import tempfile
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
_LOGGER = logging.getLogger(__name__)

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
        chunk_size = 512 * 1024 * 1024
        ONE_GIB = 1024 * 1024 * 1024
        tmp_path = None
        session_token = None

        # ------------------------------------------------------------------
        # 1) Determine the total size
        # ------------------------------------------------------------------
        if size_hint is None:
            fd, tmp_path = tempfile.mkstemp(prefix="ha-kdrive-", suffix=".tar")
            os.close(fd)

            async for chunk in await open_stream():
                with open(tmp_path, "ab") as f:
                    f.write(chunk)

            total_size = os.path.getsize(tmp_path)
            _LOGGER.critical(f"DEBUG - Size of the backup: {total_size}")
        else:
            total_size = size_hint
        
        # ------------------------------------------------------------------
        # 2) Direct upload if <= 1 Go
        # ------------------------------------------------------------------
        try:
            if total_size <= ONE_GIB:
                _LOGGER.critical(f"DEBUG - Backup less than 1GB")
                url = f"{self._base_v3}/upload?total_size={total_size}&directory_id={self._folder_id}&file_name={filename}"
                if tmp_path:
                    with open(tmp_path, "rb") as f:
                        async with self._session.post(
                            url, headers=self._headers, data=f
                        ) as resp:
                            resp.raise_for_status()
                else:
                    async with self._session.post(
                        url,
                        headers=self._headers,
                        data=await open_stream(),
                    ) as resp:
                        resp.raise_for_status()

                return
        
        except Exception:
            raise


        # ------------------------------------------------------------------
        # 3) Chunk if > 1 Go, start session
        # ------------------------------------------------------------------
        try:
            if total_size > ONE_GIB:
                _LOGGER.critical(f"DEBUG - Backup more than 1GB")

                chunk_number = math.ceil(total_size / chunk_size)
                _LOGGER.critical(f"DEBUG - Number of chunk : {chunk_number}")
                
                # --- START SESSION --- #
                start_url = f"{self._base_v3}/upload/session/start?total_size={total_size}&directory_id={self._folder_id}&file_name={filename}&total_chunks={chunk_number}"
                async with self._session.post(
                    start_url,
                    headers={**self._headers, "Content-Type": "application/json"},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                session_token = (
                    data.get("data", {}).get("session_token")
                    or data.get("session_token")
                )
                _LOGGER.critical(f"DEBUG - Token : {session_token}")
                if not session_token:
                    raise RuntimeError("Session token manquant")

                # --- SEND THE CHUNKS --- #
                async def chunk_iter():
                    if tmp_path:
                        with open(tmp_path, "rb") as f:
                            while True:
                                buf = f.read(chunk_size)
                                if not buf:
                                    break
                                yield buf
                    else:
                        buffer = bytearray()
                        async for part in await open_stream():
                            buffer.extend(part)
                            while len(buffer) >= chunk_size:
                                yield bytes(buffer[:chunk_size])
                                del buffer[:chunk_size]
                        if buffer:
                            yield bytes(buffer)

                offset = 0
                iteration = 1
                async for chunk in chunk_iter():
                    _LOGGER.critical(f"DEBUG - Iteration : {iteration}")
                    end = offset + len(chunk) - 1

                    upload_url = f"{self._base_v3}/upload/session/{session_token}/chunk?chunk_number={iteration}&chunk_size{len(chunk)}"
                    headers = {
                        **self._headers,
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total_size}",
                    }

                    async with self._session.post(
                        upload_url,
                        headers=headers,
                        data=chunk,
                    ) as resp:
                        resp.raise_for_status()

                    offset += len(chunk)
                    iteration += 1

                if offset != total_size:
                    raise RuntimeError("Upload incomplet")

                # --- CLOSE THE SESSION --- #
                _LOGGER.critical(f"DEBUG - Close session")
                finish_url = f"{self._base_v3}/upload/session/{session_token}/finish"
                async with self._session.post(
                    finish_url,
                    headers={**self._headers, "Content-Type": "application/json"},
                ) as resp:
                    resp.raise_for_status()

        # --- CANCEL THE SESSION --- #
        except Exception:
            if session_token:
                cancel_url = f"{self._base_v2}/upload/session/{session_token}"
                try:
                    async with self._session.delete(
                        cancel_url, headers=self._headers
                    ):
                      pass
                except Exception:
                    pass
            raise

        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
