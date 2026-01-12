bl_info = {
    "name": "Cell Look Shader",
    "author": "micchimouse",
    "version": (1, 5, 3),
    "blender": (4, 0, 0),
    "location": "Shader Editor > Sidebar",
    "description": "One-click creation of Cell Look material (Eevee). Settings are adjusted later in Shader Editor.",
    "category": "Material",
}

import bpy
from bpy.props import StringProperty, BoolProperty

# Fixed internal names (not shown in UI)
GROUP_NAME_DEFAULT = "CellLook_Shader_Group"
DEFAULT_MATERIAL_NAME = "CellLook_Material"


# -----------------------
# Helpers (compat)
# -----------------------

def _group_has_output_socket(ng: bpy.types.NodeTree, name: str) -> bool:
    """Best-effort check: does node group interface have an OUTPUT socket with given name?"""
    try:
        iface = getattr(ng, "interface", None)
        if iface is None:
            return False
        items = getattr(iface, "items_tree", None)
        if items is None:
            return False

        def walk(it):
            for x in it:
                yield x
                children = getattr(x, "children", None)
                if children:
                    yield from walk(children)

        for item in walk(items):
            if getattr(item, "in_out", None) == 'OUTPUT' and getattr(item, "name", None) == name:
                return True
        return False
    except Exception:
        return False


def _backup_existing_group_if_incompatible(group_name: str):
    """
    If a group exists but does NOT have 'Mask' output, rename it to keep old materials safe,
    then we'll create a new compatible group with the original name.
    """
    ng = bpy.data.node_groups.get(group_name)
    if not ng:
        return
    if _group_has_output_socket(ng, "Mask"):
        return

    base = f"{group_name}_OLD"
    new_name = base
    idx = 1
    while bpy.data.node_groups.get(new_name):
        idx += 1
        new_name = f"{base}_{idx}"
    ng.name = new_name


# -----------------------
# Node Group: Cell Look Mask (LOCKED)
# -----------------------

def get_or_create_group(group_name: str):
    """
    Creates (or reuses) a ShaderNodeTree node group that outputs a stable 0/1 mask.

    Inside:
    Diffuse -> ShaderToRGB -> RGBToBW -> Greater Than (threshold via Ramp Pos input) -> Invert (1-x)
           -> CombineColor -> Output Mask

    Note:
    - Ramp Pos exists as an input, but the Add-on UI does NOT expose it.
      Users adjust it later in the Shader Editor on the group node instance.
    """
    _backup_existing_group_if_incompatible(group_name)

    ng = bpy.data.node_groups.get(group_name)
    if ng and _group_has_output_socket(ng, "Mask"):
        return ng

    ng = bpy.data.node_groups.new(group_name, "ShaderNodeTree")
    iface = ng.interface

    # Keep Ramp Pos as group input (editable in Shader Editor later)
    in_thr = iface.new_socket(name="Ramp Pos", in_out='INPUT', socket_type='NodeSocketFloat')
    in_thr.default_value = 0.7
    in_thr.min_value = 0.0
    in_thr.max_value = 1.0

    # Output mask
    iface.new_socket(name="Mask", in_out='OUTPUT', socket_type='NodeSocketColor')

    nodes = ng.nodes
    links = ng.links

    for n in list(nodes):
        nodes.remove(n)

    n_in = nodes.new("NodeGroupInput")
    n_in.location = (-900, 0)

    n_out = nodes.new("NodeGroupOutput")
    n_out.location = (620, 0)

    n_diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    n_diffuse.location = (-700, 0)
    n_diffuse.inputs["Roughness"].default_value = 0.0
    n_diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)

    n_s2rgb = nodes.new("ShaderNodeShaderToRGB")
    n_s2rgb.location = (-480, 0)

    n_rgb2bw = nodes.new("ShaderNodeRGBToBW")
    n_rgb2bw.location = (-260, 0)

    n_gt = nodes.new("ShaderNodeMath")
    n_gt.location = (-40, 0)
    n_gt.operation = 'GREATER_THAN'

    # Fixed invert (hidden)
    n_inv = nodes.new("ShaderNodeMath")
    n_inv.location = (170, 0)
    n_inv.operation = 'SUBTRACT'
    n_inv.inputs[0].default_value = 1.0  # 1 - x

    # Convert scalar -> RGB mask for robust linking
    n_combine = nodes.new("ShaderNodeCombineColor")
    n_combine.location = (380, 0)

    # Links
    links.new(n_diffuse.outputs["BSDF"], n_s2rgb.inputs["Shader"])
    links.new(n_s2rgb.outputs["Color"], n_rgb2bw.inputs["Color"])
    links.new(n_rgb2bw.outputs["Val"], n_gt.inputs[0])

    # Ramp Pos -> threshold input
    if "Ramp Pos" in n_in.outputs:
        links.new(n_in.outputs["Ramp Pos"], n_gt.inputs[1])
    else:
        n_gt.inputs[1].default_value = 0.7

    # Invert
    links.new(n_gt.outputs[0], n_inv.inputs[1])

    # Put scalar into RGB
    def link_scalar_to_rgb(out_socket):
        for nm in ("Red", "R"):
            if nm in n_combine.inputs:
                links.new(out_socket, n_combine.inputs[nm])
                break
        for nm in ("Green", "G"):
            if nm in n_combine.inputs:
                links.new(out_socket, n_combine.inputs[nm])
                break
        for nm in ("Blue", "B"):
            if nm in n_combine.inputs:
                links.new(out_socket, n_combine.inputs[nm])
                break

    link_scalar_to_rgb(n_inv.outputs[0])

    # Output
    if "Mask" in n_out.inputs:
        links.new(n_combine.outputs["Color"], n_out.inputs["Mask"])
    else:
        links.new(n_combine.outputs["Color"], n_out.inputs[0])

    # Labels
    n_gt.label = "Threshold (Ramp Pos)"
    n_inv.label = "Invert (fixed)"
    n_combine.label = "Mask to Color"

    return ng


# -----------------------
# Material builder
# -----------------------

def create_celllook_material(mat_name: str, group_name: str = GROUP_NAME_DEFAULT):
    """
    Material:
    Group Mask -> Mix(Factor) -> Emission -> Output

    Defaults are intentionally neutral; users tweak Ramp Pos / Blend Mode / Colors later in Shader Editor.
    """
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    for n in list(nodes):
        nodes.remove(n)

    n_out = nodes.new("ShaderNodeOutputMaterial")
    n_out.location = (820, 0)

    n_em = nodes.new("ShaderNodeEmission")
    n_em.location = (600, 0)
    n_em.inputs["Strength"].default_value = 1.0

    group = get_or_create_group(group_name)
    n_group = nodes.new("ShaderNodeGroup")
    n_group.node_tree = group
    n_group.location = (0, 0)
    n_group.label = "Cell Look Mask (LOCKED)"

    # Mix node outside (so blend mode is per-material)
    n_mix = nodes.new("ShaderNodeMix")
    n_mix.location = (300, 0)
    n_mix.data_type = 'RGBA'
    n_mix.blend_type = 'MIX'  # default; adjust later in Shader Editor
    n_mix.clamp_factor = True
    if hasattr(n_mix, "clamp_result"):
        n_mix.clamp_result = False

    # Defaults: Light=white, Dark=black (adjust later)
    def set_socket_color(node, socket_candidates, value):
        for s in socket_candidates:
            if s in node.inputs:
                node.inputs[s].default_value = value
                return True
        return False

    set_socket_color(n_mix, ["A", "Color1"], (1.0, 1.0, 1.0, 1.0))
    set_socket_color(n_mix, ["B", "Color2"], (0.0, 0.0, 0.0, 1.0))

    # Group mask -> Mix factor (robust: Mask -> Color fallback)
    fac_socket = n_mix.inputs.get("Factor") or n_mix.inputs.get("Fac")
    if fac_socket:
        out_sock = n_group.outputs.get("Mask") or n_group.outputs.get("Color")
        if out_sock is None and len(n_group.outputs) > 0:
            out_sock = n_group.outputs[0]
        if out_sock is not None:
            links.new(out_sock, fac_socket)

    links.new(n_mix.outputs["Result"], n_em.inputs["Color"])
    links.new(n_em.outputs["Emission"], n_out.inputs["Surface"])

    return mat


# -----------------------
# Render Settings Apply (NEW)
# -----------------------

def _set_if_exists(obj, attr: str, value):
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
            return True
        except Exception:
            return False
    return False


def _enum_contains(render_settings, prop_name: str, identifier: str) -> bool:
    try:
        p = render_settings.bl_rna.properties[prop_name]
        return any(e.identifier == identifier for e in p.enum_items)
    except Exception:
        return False


class CELLLOOK_OT_apply_recommended_settings(bpy.types.Operator):
    bl_idname = "celllook.apply_recommended_settings"
    bl_label = "Apply Recommended Render Settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        msg = []

        # 1) Render Engine: EEVEE
        # Blender 4.x+: BLENDER_EEVEE_NEXT, older: BLENDER_EEVEE
        target = None
        if _enum_contains(scene.render, "engine", "BLENDER_EEVEE_NEXT"):
            target = "BLENDER_EEVEE_NEXT"
        elif _enum_contains(scene.render, "engine", "BLENDER_EEVEE"):
            target = "BLENDER_EEVEE"

        if target:
            if scene.render.engine != target:
                scene.render.engine = target
                msg.append(f"Render Engine -> {target}")
            else:
                msg.append("Render Engine already EEVEE")
        else:
            self.report({'WARNING'}, "Could not find an EEVEE engine enum on this Blender version.")
            # continue anyway

        # 2) Render > Sampling > Shadows: enable + Rays 1 / Steps 1
        # Property names can differ; do best-effort on scene.eevee
        ee = getattr(scene, "eevee", None)
        if ee is None:
            self.report({'WARNING'}, "scene.eevee settings not found (unexpected).")
        else:
            # enable shadows (best-effort)
            # candidates seen across versions / builds
            shadow_bool_candidates = [
                "use_shadows",
                "use_shadow",
                "use_shadow_maps",
                "use_shadow_tracing",
            ]
            enabled = False
            for a in shadow_bool_candidates:
                if _set_if_exists(ee, a, True):
                    enabled = True
                    msg.append(f"Shadows -> ON ({a})")
                    break
            if not enabled:
                msg.append("Shadows -> (property not found, skipped)")

            # Rays / Steps (best-effort)
            ray_candidates = [
                "shadow_ray_count",
                "shadow_rays",
                "shadow_ray_samples",
                "shadow_samples",
            ]
            step_candidates = [
                "shadow_step_count",
                "shadow_steps",
                "shadow_ray_steps",
            ]

            rays_set = False
            for a in ray_candidates:
                if _set_if_exists(ee, a, 1):
                    rays_set = True
                    msg.append(f"Shadows Rays -> 1 ({a})")
                    break
            if not rays_set:
                msg.append("Shadows Rays -> (property not found, skipped)")

            steps_set = False
            for a in step_candidates:
                if _set_if_exists(ee, a, 1):
                    steps_set = True
                    msg.append(f"Shadows Steps -> 1 ({a})")
                    break
            if not steps_set:
                msg.append("Shadows Steps -> (property not found, skipped)")

        # 3) Color Management > View : Standard
        # view_settings.view_transform controls View
        vs = getattr(scene, "view_settings", None)
        if vs and hasattr(vs, "view_transform"):
            try:
                vs.view_transform = "Standard"
                msg.append("View Transform -> Standard")
            except Exception:
                msg.append("View Transform -> (failed to set, skipped)")
        else:
            msg.append("View Transform -> (not available, skipped)")

        # Report (short)
        self.report({'INFO'}, " / ".join(msg[:3]) + (" ..." if len(msg) > 3 else ""))
        return {'FINISHED'}


# -----------------------
# UI / Operator
# -----------------------

README_LINES = [
    "・Render > Render Engine : EEVEE",
    "・Render > Sampling > Shadows : Check ON / Rays 1 / Steps 1",
    "・Render > Color Management > View : Standard",
    "・Data (Add a light) > Power : Large number (ex. 50000w)",
    "・Data (Add a light) > Radius > Soft Falloff : Check OFF",
]


class CellLookProps(bpy.types.PropertyGroup):
    material_name: StringProperty(
        name="Material Name",
        default=DEFAULT_MATERIAL_NAME
    )
    show_readme: BoolProperty(
        name="Recommended Settings",
        default=False,
        description="Show/hide recommended settings"
    )


class CELLLOOK_OT_create(bpy.types.Operator):
    bl_idname = "celllook.create_material"
    bl_label = "Create Cell Look Material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        p = context.scene.celllook_props

        mat = create_celllook_material(
            mat_name=p.material_name,
            group_name=GROUP_NAME_DEFAULT
        )

        obj = context.active_object
        if obj and hasattr(obj.data, "materials"):
            if obj.data.materials:
                obj.data.materials[0] = mat
            else:
                obj.data.materials.append(mat)

        self.report({'INFO'}, f"Created material: {mat.name}")
        return {'FINISHED'}


class CELLLOOK_PT_panel(bpy.types.Panel):
    bl_label = "Cell Look Shader"
    bl_idname = "CELLLOOK_PT_panel"
    bl_space_type = 'NODE_EDITOR'   # Shader Editor / Node Editor
    bl_region_type = 'UI'
    bl_category = 'Toon'
    bl_context = "shader"           # Show only in Shader context

    def draw(self, context):
        p = context.scene.celllook_props
        layout = self.layout

        # ---- Material Name (vertical layout) ----
        col = layout.column(align=True)
        col.label(text="Material Name")
        col.prop(p, "material_name", text="")

        # ---- Recommended Settings (accordion) ----
        box = layout.box()
        row = box.row(align=True)
        icon = 'TRIA_DOWN' if p.show_readme else 'TRIA_RIGHT'
        row.prop(p, "show_readme", text="Recommended Settings", icon=icon, emboss=False)

        if p.show_readme:
            col = box.column(align=True)
            for line in README_LINES:
                col.label(text=line)

            col.separator(factor=0.5)
            r = col.row()
            r.scale_y = 1.2
            r.operator("celllook.apply_recommended_settings", icon='CHECKMARK')

        layout.separator()

        # ---- Bigger create button ----
        row = layout.row()
        row.scale_y = 1.4
        row.operator("celllook.create_material", icon='MATERIAL')


classes = (
    CellLookProps,
    CELLLOOK_OT_apply_recommended_settings,
    CELLLOOK_OT_create,
    CELLLOOK_PT_panel
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.celllook_props = bpy.props.PointerProperty(type=CellLookProps)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.celllook_props


if __name__ == "__main__":
    register()
