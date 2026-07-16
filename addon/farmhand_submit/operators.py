"""Blender operators for packing, submission, polling, and cancellation."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import bpy

from .client import FarmhandClient, FarmhandError


def _preferences(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _client(context) -> FarmhandClient:
    preferences = _preferences(context)
    if not preferences:
        raise FarmhandError("Farmhand add-on preferences are unavailable.")
    return FarmhandClient(preferences.coordinator_url, preferences.farm_token)


def _redraw(context) -> None:
    if context.area:
        context.area.tag_redraw()


def _external_dependencies() -> list[str]:
    """Return assets pack_all cannot safely make portable."""

    problems: list[str] = []
    for library in bpy.data.libraries:
        problems.append(f"Linked library: {library.filepath or library.name}")
    for cache in getattr(bpy.data, "cache_files", ()):
        if getattr(cache, "filepath", ""):
            problems.append(f"External cache: {cache.filepath}")
    for font in getattr(bpy.data, "fonts", ()):
        filepath = getattr(font, "filepath", "")
        if filepath and not filepath.startswith("<") and not getattr(font, "packed_file", None):
            problems.append(f"External font: {filepath}")
    for image in getattr(bpy.data, "images", ()):
        if getattr(image, "source", "") == "MOVIE" and not getattr(image, "packed_file", None):
            problems.append(f"Video texture: {image.filepath or image.name}")
    for obj in bpy.data.objects:
        for modifier in getattr(obj, "modifiers", ()):
            point_cache = getattr(modifier, "point_cache", None)
            if point_cache and getattr(point_cache, "use_disk_cache", False):
                problems.append(f"Disk cache: {obj.name} / {modifier.name}")
            domain = getattr(modifier, "domain_settings", None)
            if domain and getattr(domain, "cache_directory", ""):
                problems.append(f"Simulation cache: {obj.name} / {modifier.name}")
        for system in getattr(obj, "particle_systems", ()):
            if getattr(system.point_cache, "use_disk_cache", False):
                problems.append(f"Particle disk cache: {obj.name} / {system.name}")
    return problems


def _frame_params(scene) -> tuple[int, int, int]:
    if scene.farmhand_use_custom_range:
        return scene.farmhand_frame_start, scene.farmhand_frame_end, scene.farmhand_frame_step
    return scene.frame_start, scene.frame_end, scene.frame_step


class _ThreadResult:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.value: Any = None
        self.error = ""

    def run(self, callback: Callable[[], Any]) -> None:
        try:
            value = callback()
            error = ""
        except FarmhandError as exc:
            value, error = None, str(exc)
        except Exception as exc:  # Thread must turn unexpected failures into a UI error.
            value, error = None, f"Unexpected Farmhand error: {exc}"
        with self.lock:
            self.value, self.error, self.done = value, error, True

    def snapshot(self) -> tuple[bool, Any, str]:
        with self.lock:
            return self.done, self.value, self.error


class FARMHAND_OT_submit(bpy.types.Operator):
    bl_idname = "farmhand.submit"
    bl_label = "Render on Farm"
    bl_description = "Pack this scene, save a temporary copy, and upload it"

    _timer = None
    _thread = None
    _result = None
    _temp_path = ""

    @classmethod
    def poll(cls, context):
        return context.scene is not None and not context.scene.farmhand_in_flight

    def execute(self, context):
        scene = context.scene
        preferences = _preferences(context)
        shared_storage = bool(preferences and preferences.shared_storage)
        if shared_storage and bpy.data.is_saved and bpy.data.is_dirty:
            scene.farmhand_error = "Save your latest changes before submitting by shared path."
            self.report({"ERROR"}, scene.farmhand_error)
            return {"CANCELLED"}
        scene.farmhand_error = ""
        if not bpy.data.is_saved:
            self.report({"ERROR"}, "Save the .blend file before submitting it to Farmhand.")
            scene.farmhand_error = "Save the .blend file before submitting."
            return {"CANCELLED"}
        try:
            client = _client(context)
        except FarmhandError as exc:
            scene.farmhand_error = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        start, end, step = _frame_params(scene)
        frame_count = ((end - start) // step) + 1 if end >= start and step > 0 else 0
        if not 1 <= frame_count <= 10_000:
            scene.farmhand_error = (
                "Frame range must be ordered, positive, and contain at most 10,000 frames."
            )
            self.report({"ERROR"}, scene.farmhand_error)
            return {"CANCELLED"}

        self._temp_path = ""
        if shared_storage:
            # Workers open the saved file straight off shared storage; textures and
            # linked assets must also resolve there.
            shared_path = bpy.data.filepath
        else:
            try:
                bpy.ops.file.pack_all()
            except RuntimeError as exc:
                scene.farmhand_error = f"Could not pack scene assets: {exc}"
                self.report({"ERROR"}, scene.farmhand_error)
                return {"CANCELLED"}

            dependencies = _external_dependencies()
            if dependencies:
                preview = "; ".join(dependencies[:3])
                if len(dependencies) > 3:
                    preview += f"; and {len(dependencies) - 3} more"
                scene.farmhand_error = f"Submission blocked by unpacked external assets: {preview}"
                self.report({"ERROR"}, scene.farmhand_error)
                return {"CANCELLED"}

            handle, self._temp_path = tempfile.mkstemp(prefix="farmhand_", suffix=".blend")
            os.close(handle)
            try:
                bpy.ops.wm.save_as_mainfile(filepath=self._temp_path, copy=True)
            except RuntimeError as exc:
                Path(self._temp_path).unlink(missing_ok=True)
                scene.farmhand_error = f"Could not save the Farmhand copy: {exc}"
                self.report({"ERROR"}, scene.farmhand_error)
                return {"CANCELLED"}

        engine = scene.render.engine
        params = {
            "name": Path(bpy.data.filepath).stem,
            "frame_start": start,
            "frame_end": end,
            "frame_step": step,
            "output_format": "PNG",
            "engine": engine,
            "blender_version": f"{bpy.app.version[0]}.{bpy.app.version[1]}",
        }

        self._result = _ThreadResult()

        def upload():
            try:
                worker_versions = client.known_worker_versions()
                if shared_storage:
                    response = client.submit_job_by_path(shared_path, params)
                else:
                    response = client.submit_job(self._temp_path, params)
                if worker_versions and params["blender_version"] not in worker_versions:
                    available = ", ".join(sorted(worker_versions))
                    response["_farmhand_warning"] = (
                        f"No known worker uses Blender {params['blender_version']} "
                        f"(known versions: {available}). The job will wait for a compatible worker."
                    )
                return response
            finally:
                if self._temp_path:
                    Path(self._temp_path).unlink(missing_ok=True)

        self._thread = threading.Thread(target=self._result.run, args=(upload,), daemon=True)
        self._thread.start()
        scene.farmhand_in_flight = True
        scene.farmhand_message = (
            "Submitting shared blend path…" if shared_storage else "Uploading packed scene…"
        )
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        done, value, error = self._result.snapshot()
        if not done:
            return {"PASS_THROUGH"}
        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.farmhand_in_flight = False
        if error:
            scene.farmhand_error = error
            scene.farmhand_message = "Submission failed"
            self.report({"ERROR"}, error)
            _redraw(context)
            return {"CANCELLED"}
        scene.farmhand_job_id = value["job_id"]
        scene.farmhand_status = "pending"
        warning = value.get("_farmhand_warning", "")
        scene.farmhand_message = warning or f"Submitted job {value['job_id']}"
        scene.farmhand_error = ""
        self.report({"WARNING"} if warning else {"INFO"}, scene.farmhand_message)
        _redraw(context)
        bpy.ops.farmhand.poll_job("INVOKE_DEFAULT")
        return {"FINISHED"}

    def cancel(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        context.scene.farmhand_in_flight = False


def _apply_status(scene, status: dict[str, Any]) -> None:
    counts = status.get("state_counts") or status.get("counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    scene.farmhand_status = str(status.get("status") or status.get("state") or "")
    scene.farmhand_pending = int(counts.get("pending", 0))
    scene.farmhand_rendering = int(counts.get("rendering", 0))
    scene.farmhand_done = int(counts.get("done", 0))
    scene.farmhand_failed = int(counts.get("failed", 0))
    counted = sum(
        (
            scene.farmhand_pending,
            scene.farmhand_rendering,
            scene.farmhand_done,
            scene.farmhand_failed,
        )
    )
    scene.farmhand_total = int(status.get("total_frames") or status.get("total") or counted)
    scene.farmhand_progress = (
        scene.farmhand_done / scene.farmhand_total if scene.farmhand_total else 0.0
    )
    scene.farmhand_message = f"{scene.farmhand_done} / {scene.farmhand_total} frames complete"


class FARMHAND_OT_poll_job(bpy.types.Operator):
    bl_idname = "farmhand.poll_job"
    bl_label = "Refresh Farmhand Job"
    bl_options = {"INTERNAL"}

    _timer = None
    _result = None
    _thread = None
    _next_poll_at = 0.0

    @classmethod
    def poll(cls, context):
        return bool(
            context.scene
            and context.scene.farmhand_job_id
            and not context.scene.farmhand_polling
        )

    def invoke(self, context, _event):
        try:
            self._farm_client = _client(context)
        except FarmhandError as exc:
            context.scene.farmhand_error = str(exc)
            return {"CANCELLED"}
        self._job_id = context.scene.farmhand_job_id
        context.scene.farmhand_polling = True
        self._result = None
        self._next_poll_at = 0.0
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _start_poll(self):
        self._result = _ThreadResult()
        self._thread = threading.Thread(
            target=self._result.run,
            args=(lambda: self._farm_client.get_job(self._job_id),),
            daemon=True,
        )
        self._thread.start()

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if context.scene.farmhand_refresh_requested:
            context.scene.farmhand_refresh_requested = False
            self._next_poll_at = 0.0
        if self._result is None and time.monotonic() >= self._next_poll_at:
            self._start_poll()
            return {"PASS_THROUGH"}
        if self._result is None:
            return {"PASS_THROUGH"}
        done, value, error = self._result.snapshot()
        if not done:
            return {"PASS_THROUGH"}
        if error:
            context.scene.farmhand_error = error
            return self._finish(context, {"CANCELLED"})
        _apply_status(context.scene, value)
        context.scene.farmhand_error = ""
        _redraw(context)
        terminal = context.scene.farmhand_status.lower() in {"complete", "cancelled", "failed"}
        if terminal:
            return self._finish(context, {"FINISHED"})
        self._result = None
        self._next_poll_at = time.monotonic() + 3.0
        return {"PASS_THROUGH"}

    def _finish(self, context, result):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        context.scene.farmhand_polling = False
        _redraw(context)
        return result

    def cancel(self, context):
        self._finish(context, {"CANCELLED"})


class FARMHAND_OT_refresh_job(bpy.types.Operator):
    bl_idname = "farmhand.refresh_job"
    bl_label = "Refresh Farmhand Job"
    bl_description = "Request an immediate background status refresh"

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.scene.farmhand_job_id)

    def execute(self, context):
        if context.scene.farmhand_polling:
            context.scene.farmhand_refresh_requested = True
        else:
            bpy.ops.farmhand.poll_job("INVOKE_DEFAULT")
        return {"FINISHED"}


class FARMHAND_OT_cancel_job(bpy.types.Operator):
    bl_idname = "farmhand.cancel_job"
    bl_label = "Cancel Job"
    bl_description = "Stop Farmhand from assigning any more frames"

    _timer = None
    _result = None

    @classmethod
    def poll(cls, context):
        return bool(
            context.scene
            and context.scene.farmhand_job_id
            and not context.scene.farmhand_in_flight
        )

    def execute(self, context):
        try:
            client = _client(context)
        except FarmhandError as exc:
            context.scene.farmhand_error = str(exc)
            return {"CANCELLED"}
        job_id = context.scene.farmhand_job_id
        self._result = _ThreadResult()
        threading.Thread(
            target=self._result.run, args=(lambda: client.cancel_job(job_id),), daemon=True
        ).start()
        context.scene.farmhand_in_flight = True
        context.scene.farmhand_message = "Cancelling job…"
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        done, _value, error = self._result.snapshot()
        if not done:
            return {"PASS_THROUGH"}
        context.window_manager.event_timer_remove(self._timer)
        context.scene.farmhand_in_flight = False
        if error:
            context.scene.farmhand_error = error
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        context.scene.farmhand_status = "cancelled"
        context.scene.farmhand_message = "Job cancelled"
        context.scene.farmhand_error = ""
        _redraw(context)
        return {"FINISHED"}

    def cancel(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        context.scene.farmhand_in_flight = False


CLASSES = (
    FARMHAND_OT_submit,
    FARMHAND_OT_poll_job,
    FARMHAND_OT_refresh_job,
    FARMHAND_OT_cancel_job,
)
