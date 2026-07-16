from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from addon.farmhand_submit.client import (
    FarmhandClient,
    FarmhandError,
    MultipartFileBody,
    encode_multipart,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]


def test_encode_multipart_has_contract_names_and_crlf():
    body = encode_multipart(
        {"params": '{"name":"Cube"}'},
        {"blend_file": ("cube.blend", b"BLENDER", "application/octet-stream")},
        boundary="test-boundary",
    )

    assert body.startswith(b"--test-boundary\r\n")
    assert b'Content-Disposition: form-data; name="params"\r\n\r\n' in body
    assert b'Content-Disposition: form-data; name="blend_file"; filename="cube.blend"' in body
    assert b"Content-Type: application/octet-stream\r\n\r\nBLENDER\r\n" in body
    assert body.endswith(b"--test-boundary--\r\n")


def test_streaming_multipart_length_matches_serialized_body(tmp_path):
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"blend-data")
    fields = {"params": '{"frame_start":1}'}
    stream = MultipartFileBody(
        fields=fields,
        file_field="blend_file",
        file_path=blend,
        boundary="fixed",
        chunk_size=3,
    )
    expected = encode_multipart(
        fields,
        {"blend_file": ("scene.blend", b"blend-data", "application/octet-stream")},
        boundary="fixed",
    )

    assert b"".join(stream) == expected
    assert stream.content_length == len(expected)
    assert sha256_file(blend) == "0bec25fcceae863db21f5d26f8abc94f16b115be71d6bcaa9cca7f5635220824"


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.__class__.requests.append((self.path, self.headers, body))
        if self.path == "/jobs":
            response = b'{"job_id":"abc123"}'
            self.send_response(201)
        else:
            response = b'{"status":"cancelled"}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        if self.path == "/jobs/broken":
            response = b'{"detail":"job not found"}'
            self.send_response(404)
        elif self.path == "/jobs/one":
            response = b'{"workers":[{"worker_id":"shop","blender_version":"4.2"}]}'
            self.send_response(200)
        else:
            response = b'[{"id":"one","name":"Scene"}]'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        pass


@pytest.fixture
def coordinator():
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_submit_job_sends_api_v1_multipart(coordinator, tmp_path):
    blend = tmp_path / "textured.blend"
    blend.write_bytes(b"packed blend bytes")
    params = {
        "name": "Textured Scene",
        "frame_start": 1,
        "frame_end": 10,
        "frame_step": 2,
        "output_format": "PNG",
        "engine": "BLENDER_EEVEE_NEXT",
        "blender_version": "4.5",
    }

    result = FarmhandClient(coordinator, "secret").submit_job(blend, params)

    assert result == {"job_id": "abc123"}
    path, headers, body = _Handler.requests[0]
    assert path == "/jobs"
    assert headers["X-Farm-Token"] == "secret"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=farmhand-")
    assert int(headers["Content-Length"]) == len(body)
    assert json.dumps(params, separators=(",", ":"), sort_keys=True).encode() in body
    assert b'name="blend_file"; filename="textured.blend"' in body
    assert b"packed blend bytes" in body


def test_submit_job_by_path_sends_only_params(coordinator):
    params = {
        "name": "NAS Scene",
        "frame_start": 1,
        "frame_end": 10,
        "frame_step": 1,
        "output_format": "PNG",
        "engine": "CYCLES",
        "blender_version": "4.5",
    }

    result = FarmhandClient(coordinator, "secret").submit_job_by_path(
        "/Volumes/renders/shot42.blend", params
    )

    assert result == {"job_id": "abc123"}
    path, headers, body = _Handler.requests[0]
    assert path == "/jobs"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=farmhand-")
    assert b'"blend_path":"/Volumes/renders/shot42.blend"' in body
    assert b'name="blend_file"' not in body


def test_shared_storage_checks_dirty_state_before_writing_scene_status():
    source = (ROOT / "addon/farmhand_submit/operators.py").read_text()
    execute = source[source.index("    def execute(self, context):") :]

    assert execute.index("bpy.data.is_dirty") < execute.index('scene.farmhand_error = ""')


def test_cancel_escapes_job_id_and_uses_token(coordinator):
    FarmhandClient(coordinator, "secret").cancel_job("id/with space")
    path, headers, body = _Handler.requests[0]
    assert path == "/jobs/id%2Fwith%20space/cancel"
    assert headers["X-Farm-Token"] == "secret"
    assert body == b""


def test_configuration_and_missing_params_are_clean_errors(tmp_path):
    with pytest.raises(FarmhandError, match="Coordinator URL"):
        FarmhandClient("", "secret")
    with pytest.raises(FarmhandError, match="Farm token"):
        FarmhandClient("http://farm", "")
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x")
    with pytest.raises(FarmhandError, match="Missing job parameters"):
        FarmhandClient("http://farm", "secret").submit_job(blend, {"name": "nope"})


def test_status_errors_are_clean_and_worker_versions_are_discovered(coordinator):
    client = FarmhandClient(coordinator, "secret")
    assert client.known_worker_versions() == {"4.2"}
    with pytest.raises(FarmhandError, match="HTTP 404: job not found"):
        client.get_job("broken")
