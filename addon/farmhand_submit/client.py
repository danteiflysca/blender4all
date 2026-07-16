"""Small, dependency-free client for the Farmhand coordinator API.

This module deliberately has no Blender imports so its wire format can be
tested with ordinary Python and reused by Blender's background threads.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class FarmhandError(RuntimeError):
    """A coordinator error that is safe to show in Blender's UI."""


def _part_header(name: str, filename: str | None = None, content_type: str | None = None) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        # Blender filenames cannot contain CR/LF, but strip them defensively.
        safe_name = filename.replace("\r", "_").replace("\n", "_").replace('"', "_")
        disposition += f'; filename="{safe_name}"'
    lines = [disposition]
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def encode_multipart(
    fields: Mapping[str, str],
    files: Mapping[str, tuple[str, bytes, str]],
    *,
    boundary: str,
) -> bytes:
    """Serialize a multipart body; primarily useful for focused wire tests."""

    marker = f"--{boundary}\r\n".encode("ascii")
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend((marker, _part_header(name), value.encode("utf-8"), b"\r\n"))
    for name, (filename, data, content_type) in files.items():
        chunks.extend((marker, _part_header(name, filename, content_type), data, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


class MultipartFileBody:
    """A repeatable iterable that streams one file in a multipart request."""

    def __init__(
        self,
        *,
        fields: Mapping[str, str],
        file_field: str,
        file_path: str | os.PathLike[str],
        boundary: str,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.boundary = boundary
        self.file_path = Path(file_path)
        self.chunk_size = chunk_size
        marker = f"--{boundary}\r\n".encode("ascii")

        prefix: list[bytes] = []
        for name, value in fields.items():
            prefix.extend((marker, _part_header(name), value.encode("utf-8"), b"\r\n"))
        prefix.extend(
            (
                marker,
                _part_header(file_field, self.file_path.name, "application/octet-stream"),
            )
        )
        self._prefix = b"".join(prefix)
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        self.content_length = len(self._prefix) + self.file_path.stat().st_size + len(self._suffix)

    def __iter__(self) -> Iterator[bytes]:
        yield self._prefix
        with self.file_path.open("rb") as source:
            while chunk := source.read(self.chunk_size):
                yield chunk
        yield self._suffix


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class FarmhandClient:
    """HTTP client for the add-on-facing portion of Farmhand API v1."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        base_url = base_url.strip().rstrip("/")
        token = token.strip()
        if not base_url:
            raise FarmhandError("Coordinator URL is not configured.")
        if not token:
            raise FarmhandError("Farm token is not configured.")
        if not base_url.startswith(("http://", "https://")):
            raise FarmhandError("Coordinator URL must begin with http:// or https://.")
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        request_headers = {"Accept": "application/json", "X-Farm-Token": self.token}
        request_headers.update(headers or {})
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - configured LAN URL
                payload = response.read()
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                detail = parsed.get("detail", body) if isinstance(parsed, dict) else body
            except (OSError, UnicodeError, json.JSONDecodeError):
                detail = exc.reason
            raise FarmhandError(f"Coordinator returned HTTP {exc.code}: {detail}") from None
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise FarmhandError(f"Could not reach coordinator: {reason}") from None
        except (TimeoutError, OSError) as exc:
            raise FarmhandError(f"Coordinator request failed: {exc}") from None

        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise FarmhandError("Coordinator returned an invalid JSON response.") from None

    @staticmethod
    def _check_params(params: Mapping[str, Any]) -> None:
        required = {
            "name",
            "frame_start",
            "frame_end",
            "frame_step",
            "output_format",
            "engine",
            "blender_version",
        }
        missing = sorted(required.difference(params))
        if missing:
            raise FarmhandError(f"Missing job parameters: {', '.join(missing)}")

    def submit_job(
        self, blend_path: str | os.PathLike[str], params: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._check_params(params)
        path = Path(blend_path)
        if not path.is_file():
            raise FarmhandError(f"Blend copy does not exist: {path}")

        params_json = json.dumps(dict(params), separators=(",", ":"), sort_keys=True)
        seed = os.urandom(32) + params_json.encode("utf-8")
        boundary = "farmhand-" + hashlib.sha256(seed).hexdigest()
        body = MultipartFileBody(
            fields={"params": params_json},
            file_field="blend_file",
            file_path=path,
            boundary=boundary,
        )
        result = self._request(
            "POST",
            "/jobs",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body.content_length),
            },
        )
        if not isinstance(result, dict) or not result.get("job_id"):
            raise FarmhandError("Coordinator accepted the upload but returned no job ID.")
        return result

    def submit_job_by_path(
        self, blend_path: str | os.PathLike[str], params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Submit a job whose .blend lives on storage shared with the workers.

        Only the path is sent; nothing is uploaded. The path must resolve on
        every worker machine.
        """
        self._check_params(params)
        payload = dict(params, blend_path=str(blend_path))
        params_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        seed = os.urandom(32) + params_json.encode("utf-8")
        boundary = "farmhand-" + hashlib.sha256(seed).hexdigest()
        body = encode_multipart({"params": params_json}, {}, boundary=boundary)
        result = self._request(
            "POST",
            "/jobs",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        if not isinstance(result, dict) or not result.get("job_id"):
            raise FarmhandError("Coordinator accepted the job but returned no job ID.")
        return result

    def list_jobs(self) -> Any:
        return self._request("GET", "/jobs")

    def known_worker_versions(self) -> set[str]:
        """Return worker versions exposed by current coordinator job data.

        API v1 exposes workers on job detail rather than through a dedicated
        capabilities endpoint. Inspect the summary response first, then one
        available job detail; job detail includes workers seen by the
        coordinator.
        """

        jobs = self.list_jobs()

        def versions_in(value: Any, *, under_workers: bool = False) -> set[str]:
            found: set[str] = set()
            if isinstance(value, dict):
                for key, child in value.items():
                    is_workers = under_workers or key == "workers"
                    if is_workers and key == "blender_version" and isinstance(child, str):
                        found.add(child)
                    else:
                        found.update(versions_in(child, under_workers=is_workers))
            elif isinstance(value, list):
                for child in value:
                    found.update(versions_in(child, under_workers=under_workers))
            return found

        versions = versions_in(jobs)
        if versions:
            return versions
        summaries = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        if isinstance(summaries, list):
            for summary in summaries:
                if isinstance(summary, dict):
                    job_id = summary.get("id") or summary.get("job_id")
                    if job_id:
                        return versions_in(self.get_job(str(job_id)))
        return set()

    def get_job(self, job_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/jobs/{quote(job_id, safe='')}")
        if not isinstance(result, dict):
            raise FarmhandError("Coordinator returned invalid job status.")
        return result

    def cancel_job(self, job_id: str) -> Any:
        return self._request("POST", f"/jobs/{quote(job_id, safe='')}/cancel", data=b"")
