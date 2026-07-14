"""Farmhand N-panel UI."""

from __future__ import annotations

import bpy


def _preferences(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


class FARMHAND_PT_submit(bpy.types.Panel):
    bl_label = "Render on Farm"
    bl_idname = "FARMHAND_PT_submit"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Farmhand"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        preferences = _preferences(context)
        configured = bool(
            preferences
            and preferences.coordinator_url.strip()
            and preferences.farm_token.strip()
        )
        if not configured:
            warning = layout.row()
            warning.alert = True
            warning.label(text="Set coordinator URL and token in Add-on Preferences", icon="ERROR")

        frame_box = layout.box()
        frame_box.prop(scene, "farmhand_use_custom_range")
        column = frame_box.column(align=True)
        if scene.farmhand_use_custom_range:
            column.prop(scene, "farmhand_frame_start")
            column.prop(scene, "farmhand_frame_end")
            column.prop(scene, "farmhand_frame_step")
        else:
            column.prop(scene, "frame_start", text="Start")
            column.prop(scene, "frame_end", text="End")
            column.prop(scene, "frame_step", text="Step")

        submit = layout.row()
        submit.enabled = configured and not scene.farmhand_in_flight
        submit.operator("farmhand.submit", icon="RENDER_ANIMATION")

        if scene.farmhand_job_id:
            box = layout.box()
            box.label(text=f"Job: {scene.farmhand_job_id}")
            if scene.farmhand_status:
                box.label(text=f"State: {scene.farmhand_status}")
            box.prop(
                scene,
                "farmhand_progress",
                text=f"{scene.farmhand_done} / {scene.farmhand_total}",
            )
            counts = box.row(align=True)
            counts.label(text=f"Pending {scene.farmhand_pending}")
            counts.label(text=f"Rendering {scene.farmhand_rendering}")
            counts.label(text=f"Failed {scene.farmhand_failed}")
            actions = box.row(align=True)
            actions.enabled = not scene.farmhand_in_flight
            actions.operator("farmhand.refresh_job", text="Refresh", icon="FILE_REFRESH")
            actions.operator("farmhand.cancel_job", text="Cancel Job", icon="CANCEL")

        if scene.farmhand_message:
            layout.label(text=scene.farmhand_message, icon="INFO")
        if scene.farmhand_error:
            error = layout.box()
            error.alert = True
            error.label(text=scene.farmhand_error, icon="ERROR")


CLASSES = (FARMHAND_PT_submit,)
