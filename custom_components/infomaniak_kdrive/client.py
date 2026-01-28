
from __future__ import annotations
import hashlib
import math
from typing import AsyncIterator, Dict, List, Optional
import os
import tempfile
import aiohttp

# import logging
# logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
# _LOGGER = logging.getLogger(__name__)

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
        ONE_GIB = 900 * 1024 * 1024 # 900 MiB
        chunk_size = 5 * 1024 * 1024  # 5 MiB
        session_token = None
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=True)) as upload_session:

            # ------------------------------------------------------------------
            # 1) Determine the total size
            # ------------------------------------------------------------------
            if size_hint is None:
                total_size = ONE_GIB + 1  # Force chunked upload
            else:
                total_size = size_hint
            
            try:
                # ------------------------------------------------------------------
                # 2a) Direct upload if <= 1 Go (900 MiB in reality)
                # ------------------------------------------------------------------
                if total_size <= ONE_GIB:
                    url = f"{self._base_v3}/upload?total_size={total_size}&directory_id={self._folder_id}&file_name={filename}"
                    async with upload_session.post(url, headers=self._headers, data=await open_stream()
                    ) as resp:
                        resp.raise_for_status()

                # ------------------------------------------------------------------
                # 2b) Chunked upload if > 1 Go (900 MiB in reality)
                # ------------------------------------------------------------------
                else:
                    # --- WRITE THE WHOLE STREAM TO DISK FIRST (no RAM buffering) --- #
                    fd, tmp_path = tempfile.mkstemp(prefix="ha-kdrive-", suffix=".bin",  dir="/media")
                    os.close(fd)
                    try:
                        with open(tmp_path, "ab") as f:
                            async for part in await open_stream():
                                f.write(part)
                        total_size = os.path.getsize(tmp_path)
                    except Exception:
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                        raise
                                        
                    # --- START SESSION --- #
                    url = f"{self._base_v3}/upload/session/start"
                    payload = {
                        "directory_id": self._folder_id,
                        "file_name": filename,
                        "total_size": total_size,
                        "total_chunks": math.ceil(total_size / chunk_size),
                    }
                    async with upload_session.post(url, headers={**self._headers, "Content-Type": "application/json"}, json=payload
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    # --- EXTRACT SESSION TOKEN & URL UPLOAD --- #
                    session_token = data.get("data", {}).get("token")
                    upload_url_session = data.get("data", {}).get("upload_url")                    
                    if not session_token:
                        raise RuntimeError("Session token manquant")
                    
                    # --- READ THE FILE BY CHUNKS & CALCULATE THE SHA256 --- #
                    sha256_file = hashlib.sha256()
                    async def chunk_iter():
                        with open(tmp_path, "rb") as f:
                            while True:
                                buf = f.read(chunk_size)
                                sha256_file.update(buf)
                                if not buf:
                                    break
                                yield buf
                    
                    # --- LOOK TO UPLOAD EACH CHUNK --- #
                    iteration = 0
                    async for chunk in chunk_iter():
                        iteration += 1
                        url = f"{upload_url_session}/3/drive/{self._drive_id}/upload/session/{session_token}/chunk"                        
                        params = {
                            "chunk_number": iteration,
                            "chunk_size": len(chunk),
                            "chunk_hash": f"sha256:{hashlib.sha256(chunk).hexdigest()}",
                        }
                        async with upload_session.post(url, headers=self._headers, params=params, data=chunk
                        ) as resp:
                            resp.raise_for_status()
                            data = await resp.json()
                    
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                  
                    # --- CLOSE THE SESSION --- #
                    url = f"{self._base_v3}/upload/session/{session_token}/finish?with=capabilities,supported_by,conversion_capabilities,users,teams,path,parents,parents.capabilities,parents.users,parents.teams,parents.path"
                    params = {
                        "total_chunk_hash": f"sha256:{sha256_file.hexdigest()}",
                    }
                    async with upload_session.post(url, headers=self._headers, params=params,
                    ) as resp: 
                       resp.raise_for_status()
                       data = await resp.json()

            # --- CANCEL THE SESSION --- #
            except Exception:
                if session_token:
                    cancel_url = f"{self._base_v2}/upload/session/{session_token}"
                    try:
                        async with upload_session.delete(
                            cancel_url, headers=self._headers
                        ):
                          pass
                    except Exception:
                        pass
                raise
            
            # --- REMOVE THE BACKUP FILE IN MEDIA FOLDER --- #
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
