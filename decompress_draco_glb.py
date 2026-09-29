"""
decompress_draco_glb.py
=======================
Blender background script: imports a GLB that uses KHR_draco_mesh_compression
and re-exports it as a plain GLB (no Draco) that Godot can load.

Usage (called by main.py via subprocess):
    blender --background --python decompress_draco_glb.py -- <input.glb> <output.glb>
"""

import bpy
import sys
import os

def main():
    # Parse args after '--'
    argv = sys.argv
    try:
        separator = argv.index("--")
        args = argv[separator + 1:]
    except ValueError:
        print("ERROR: No arguments provided after '--'")
        sys.exit(1)

    if len(args) < 2:
        print(f"ERROR: Expected <input_glb> <output_glb>, got: {args}")
        sys.exit(1)

    input_path  = args[0]
    output_path = args[1]

    if not os.path.exists(input_path):
        print(f"ERROR: Input file not found: {input_path}")
        sys.exit(1)

    print(f"[decompress_draco_glb] Input:  {input_path}")
    print(f"[decompress_draco_glb] Output: {output_path}")

    # Clear the default scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Import the Draco-compressed GLB
    try:
        bpy.ops.import_scene.gltf(filepath=input_path)
        print(f"[decompress_draco_glb] Import OK — {len(bpy.data.objects)} objects loaded")
    except Exception as e:
        print(f"ERROR: Import failed: {e}")
        sys.exit(1)

    if len(bpy.data.objects) == 0:
        print("ERROR: No objects were imported — GLB may be corrupt or empty")
        sys.exit(1)

    # Export as plain GLB (Draco disabled by default in Blender's exporter)
    try:
        bpy.ops.export_scene.gltf(
            filepath=output_path,
            export_format='GLB',
            use_selection=False,
            export_draco_mesh_compression_enable=False,  # Ensure no Draco on output
            export_apply=False,
        )
        print(f"[decompress_draco_glb] Export OK → {output_path}")
    except Exception as e:
        print(f"ERROR: Export failed: {e}")
        sys.exit(1)

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        print("ERROR: Output file was not created or is empty")
        sys.exit(1)

    print(f"[decompress_draco_glb] SUCCESS — {os.path.getsize(output_path):,} bytes written")
    sys.exit(0)

main()
