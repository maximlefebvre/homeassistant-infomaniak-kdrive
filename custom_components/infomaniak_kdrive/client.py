
from __future__ import annotations
import hashlib
import logging
import math
from typing import AsyncIterator, Dict, List, Optional
import os
import tempfile
import aiohttp

_LOGGER = logging.getLogger(__name__)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# No overall timeout for uploads (they can legitimately run for a long time),
# but keep sensible limits on the initial connection and on socket reads.
upload_timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)

# Safety limit on how many folder listing pages we will follow.
MAX_LIST_PAGES = 50

def _extract_file_id(payload) -> Optional[int]:
    """Read the file id out of an upload response, never raising."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    file_id = data.get("id")
    return file_id if isinstance(file_id, int) else None


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
        """List every file in the folder, following pagination.

        A partial listing is silently destructive here: a sidecar missing from
        the listing makes its backup fall back to the degraded metadata encoded
        in the filename. So we walk every page, by cursor when the API provides
        one, by page number otherwise.
        """
        url = f"{self._base_v3}/files/{self._folder_id}/files"
        items: List[Dict] = []
        seen: set = set()
        params: Optional[Dict[str, str]] = None  # first request: no parameters

        for _ in range(MAX_LIST_PAGES):
            try:
                async with self._session.get(url, headers=self._headers, params=params) as resp:
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        _LOGGER.debug("kDrive listing %s -> %s: %s", resp.url, resp.status, body)
                    resp.raise_for_status()
                    payload = await resp.json()
            except Exception:
                if not items:
                    raise  # the first page failed: that is a real error
                # A later page failed: a partial listing beats a dead agent,
                # but it must not go unnoticed.
                _LOGGER.warning(
                    "Partial kDrive folder listing (%d files so far): pagination request failed. "
                    "Backups whose metadata sidecar is missing from the listing will show "
                    "incomplete information",
                    len(items), exc_info=True,
                )
                break

            batch = payload.get("data")
            if not isinstance(batch, list):
                break
            added = 0
            for it in batch:
                key = it.get("id")
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                items.append(it)
                added += 1
            # The server ignored our paging (same items returned): stop
            # rather than loop forever on the same page.
            if batch and added == 0:
                break

            # We only paginate according to what the response itself
            # advertises; no parameter is ever sent speculatively.
            params = self._next_page_params(payload, params)
            if params is None:
                break

        # Exclude directories rather than requiring type == "file": an
        # unexpected or missing type must not make a file disappear.
        return [it for it in items if it.get("type") not in ("dir", "directory")]

    @staticmethod
    def _next_page_params(payload: Dict, current: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        """Parameters for the next page, or None when there is none."""
        cursor = payload.get("cursor")
        has_more = payload.get("has_more")
        if has_more is False:
            return None
        if has_more and cursor and (current or {}).get("cursor") != cursor:
            return {"cursor": str(cursor)}

        pages = payload.get("pages") or payload.get("total_pages")
        page = payload.get("page") or (current or {}).get("page") or 1
        try:
            page, pages = int(page), int(pages) if pages else 0
        except (TypeError, ValueError):
            return None
        if pages and page < pages:
            return {"page": str(page + 1)}
        return None

    async def upload_bytes(self, *, filename: str, data: bytes) -> Optional[int]:
        """Upload a small in-memory payload (used for the backup metadata sidecars).

        Returns the id of the created file, or None if the API did not report it.
        """
        url = f"{self._base_v3}/upload"
        params = {
            "total_size": str(len(data)),
            "directory_id": str(self._folder_id),
            "file_name": filename,
        }
        async with self._session.post(url, headers=self._headers, params=params, data=data) as resp:
            resp.raise_for_status()
            try:
                payload = await resp.json()
            except Exception:
                return None
        return _extract_file_id(payload)

    def _download_url(self, file_id: int) -> str:
        """Download URL for a file.

        This endpoint lives on API v2: the official kDrive client builds it as
        ApiRoutes.downloadFile = fileURLV2 + "/download". The v3 equivalent
        answers 404.
        """
        return f"{self._base_v2}/files/{file_id}/download"

    async def _open_download(self, file_id: int, extra_headers: Optional[Dict] = None):
        """Open a download response. The caller is responsible for releasing it."""
        headers = {**self._headers, **(extra_headers or {})}
        resp = await self._session.get(self._download_url(file_id), headers=headers)
        # raise_for_status() releases the connection before raising.
        resp.raise_for_status()
        return resp

    async def download_file_bytes(self, file_id: int) -> bytes:
        resp = await self._open_download(file_id)
        try:
            return await resp.read()
        finally:
            resp.release()

    async def download_file_head(self, file_id: int, size: int) -> bytes:
        """Read only the first `size` bytes of a file.

        Tries a Range request first. If the server rejects it, falls back to a
        plain request and stops after `size` bytes: the whole archive is never
        downloaded, even without Range support.
        """
        last_err: Optional[Exception] = None
        for extra in ({"Range": f"bytes=0-{size - 1}"}, None):
            try:
                resp = await self._open_download(file_id, extra)
            except Exception as err:
                last_err = err
                continue
            try:
                # A server ignoring Range answers 200 with the whole file:
                # stop at the first bytes regardless.
                return await resp.content.read(size)
            finally:
                resp.release()
        raise last_err or RuntimeError(f"Could not read the head of file {file_id}")

    async def get_file_size(self, file_id: int) -> int:
        try:
            async with self._session.head(self._download_url(file_id), headers=self._headers) as resp:
                if resp.status < 400:
                    cl = resp.headers.get('Content-Length')
                    if cl is not None:
                        return int(cl)
        except Exception:
            pass
        try:
            resp = await self._open_download(file_id)
            try:
                cl = resp.headers.get('Content-Length')
                if cl is not None:
                    return int(cl)
            finally:
                resp.release()
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
        resp = await self._open_download(file_id)
        # try/finally: the response is released even if the consumer
        # abandons the download halfway through.
        try:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            resp.release()

    async def upload_stream_to_folder(self, *, filename: str, open_stream, size_hint: Optional[int] = None) -> Optional[int]:
        """Upload the backup archive. Returns the id of the created file if known."""
        ONE_GIB = 900 * 1024 * 1024 # 900 MiB
        chunk_size = 5 * 1024 * 1024  # 5 MiB
        session_token = None
        tmp_path = None
        uploaded_id: Optional[int] = None
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
                    url = f"{self._base_v3}/upload"
                    params = {
                        "total_size": str(total_size),
                        "directory_id": str(self._folder_id),
                        "file_name": filename,
                    }
                    async with upload_session.post(url, headers=self._headers, params=params, data=await open_stream(), timeout=upload_timeout
                    ) as resp:
                        resp.raise_for_status()
                        try:
                            data = await resp.json()
                        except Exception:
                            data = {}
                    uploaded_id = _extract_file_id(data)

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
                    uploaded_id = _extract_file_id(data)

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

            return uploaded_id
