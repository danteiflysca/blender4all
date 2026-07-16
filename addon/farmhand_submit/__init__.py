"""Farmhand Blender add-on entry point."""

bl_info = {
    "name": "Farmhand Submit",
    "author": "Farmhand",
    "version": (0, 1, 1),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Farmhand",
    "description": "Pack and submit Blender scenes to a Farmhand render coordinator",
    "category": "Render",
}

try:
    import bpy
except ModuleNotFoundError:  # Allows importing farmhand_submit.client in normal Python.
    bpy = None


if bpy is not None:
    from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

    from . import operators, panel

    class FARMHAND_Preferences(bpy.types.AddonPreferences):
        bl_idname = __package__ or __name__

        coordinator_url: StringProperty(
            name="Coordinator URL",
            description="Farmhand coordinator, for example http://192.168.1.20:8420",
            default="",
        )
        farm_token: StringProperty(
            name="Farm Token",
            description="Value sent in the X-Farm-Token header",
            default="",
            subtype="PASSWORD",
        )
        shared_storage: BoolProperty(
            name="Shared Storage (NAS)",
            description=(
                "Submit the saved .blend's path instead of uploading a packed copy. "
                "The file and its textures must be reachable at the same path on every worker"
            ),
            default=False,
        )

        def draw(self, _context):
            layout = self.layout
            layout.prop(self, "coordinator_url")
            layout.prop(self, "farm_token")
            layout.prop(self, "shared_storage")


    _CLASSES = (FARMHAND_Preferences, *operators.CLASSES, *panel.CLASSES)


def register() -> None:
    if bpy is None:
        raise RuntimeError("Farmhand Submit can only be registered inside Blender")
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    scene = bpy.types.Scene
    scene.farmhand_job_id = StringProperty(name="Farmhand Job ID", default="")
    scene.farmhand_status = StringProperty(name="Farmhand Status", default="")
    scene.farmhand_message = StringProperty(name="Farmhand Message", default="")
    scene.farmhand_error = StringProperty(name="Farmhand Error", default="")
    scene.farmhand_in_flight = BoolProperty(options={"SKIP_SAVE"}, default=False)
    scene.farmhand_polling = BoolProperty(options={"SKIP_SAVE"}, default=False)
    scene.farmhand_refresh_requested = BoolProperty(options={"SKIP_SAVE"}, default=False)
    scene.farmhand_use_custom_range = BoolProperty(name="Custom Frame Range", default=False)
    scene.farmhand_frame_start = IntProperty(name="Start", default=1)
    scene.farmhand_frame_end = IntProperty(name="End", default=250)
    scene.farmhand_frame_step = IntProperty(name="Step", default=1, min=1)
    scene.farmhand_pending = IntProperty(name="Pending", default=0, min=0)
    scene.farmhand_rendering = IntProperty(name="Rendering", default=0, min=0)
    scene.farmhand_done = IntProperty(name="Done", default=0, min=0)
    scene.farmhand_failed = IntProperty(name="Failed", default=0, min=0)
    scene.farmhand_total = IntProperty(name="Total", default=0, min=0)
    scene.farmhand_progress = FloatProperty(
        name="Progress", default=0.0, min=0.0, max=1.0, subtype="FACTOR"
    )
    for current_scene in bpy.data.scenes:
        current_scene.farmhand_error = ""


def unregister() -> None:
    if bpy is None:
        return
    scene = bpy.types.Scene
    for name in (
        "farmhand_progress",
        "farmhand_total",
        "farmhand_failed",
        "farmhand_done",
        "farmhand_rendering",
        "farmhand_pending",
        "farmhand_frame_step",
        "farmhand_frame_end",
        "farmhand_frame_start",
        "farmhand_use_custom_range",
        "farmhand_polling",
        "farmhand_refresh_requested",
        "farmhand_in_flight",
        "farmhand_error",
        "farmhand_message",
        "farmhand_status",
        "farmhand_job_id",
    ):
        if hasattr(scene, name):
            delattr(scene, name)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
