import json
import os
import struct
import sys
from typing import Dict, Tuple

import DracoPy
import numpy as np


GLTF_COMPONENT_TYPES = {
    np.dtype(np.int8): 5120,
    np.dtype(np.uint8): 5121,
    np.dtype(np.int16): 5122,
    np.dtype(np.uint16): 5123,
    np.dtype(np.uint32): 5125,
    np.dtype(np.float32): 5126,
}


def _pad(data: bytes, pad_byte: bytes) -> bytes:
    remainder = len(data) % 4
    if remainder == 0:
        return data
    return data + pad_byte * (4 - remainder)


def _read_glb(path: str):
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"glTF":
            raise ValueError("Not a GLB file")
        version = struct.unpack("<I", f.read(4))[0]
        if version != 2:
            raise ValueError(f"Unsupported GLB version: {version}")
        _total_length = struct.unpack("<I", f.read(4))[0]

        json_length = struct.unpack("<I", f.read(4))[0]
        json_type = f.read(4)
        if json_type != b"JSON":
            raise ValueError("Missing JSON chunk")
        json_bytes = f.read(json_length)
        json_doc = json.loads(json_bytes.decode("utf-8", errors="ignore"))

        bin_length = struct.unpack("<I", f.read(4))[0]
        bin_type = f.read(4)
        if bin_type != b"BIN\x00":
            raise ValueError("Missing BIN chunk")
        bin_bytes = f.read(bin_length)

    return json_doc, bytearray(bin_bytes)


def _write_glb(path: str, json_doc: dict, bin_bytes: bytes):
    json_bytes = json.dumps(json_doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    json_bytes = _pad(json_bytes, b" ")
    bin_bytes = _pad(bin_bytes, b"\x00")
    total_length = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)

    with open(path, "wb") as f:
        f.write(b"glTF")
        f.write(struct.pack("<I", 2))
        f.write(struct.pack("<I", total_length))
        f.write(struct.pack("<I", len(json_bytes)))
        f.write(b"JSON")
        f.write(json_bytes)
        f.write(struct.pack("<I", len(bin_bytes)))
        f.write(b"BIN\x00")
        f.write(bin_bytes)


def _glb_type_from_shape(array: np.ndarray) -> str:
    if array.ndim == 1 or array.shape[1] == 1:
        return "SCALAR"
    if array.shape[1] == 2:
        return "VEC2"
    if array.shape[1] == 3:
        return "VEC3"
    if array.shape[1] == 4:
        return "VEC4"
    raise ValueError(f"Unsupported accessor shape: {array.shape}")


def _component_type_from_dtype(dtype: np.dtype, *, for_indices: bool = False) -> int:
    dtype = np.dtype(dtype)
    if for_indices:
        if dtype.itemsize <= 2:
            return 5123
        return 5125
    if dtype in GLTF_COMPONENT_TYPES:
        return GLTF_COMPONENT_TYPES[dtype]
    return 5126


def _append_blob(bin_bytes: bytearray, payload: bytes) -> Tuple[int, int]:
    while len(bin_bytes) % 4 != 0:
        bin_bytes.append(0)
    offset = len(bin_bytes)
    bin_bytes.extend(payload)
    return offset, len(payload)


def _append_accessor(json_doc: dict, bin_bytes: bytearray, array: np.ndarray, *, target: int | None = None, semantic: str | None = None, for_indices: bool = False) -> int:
    array = np.asarray(array)
    if array.size == 0:
        raise ValueError("Cannot create accessor from empty array")

    if for_indices:
        array = np.asarray(array, dtype=np.uint32 if array.max() > 65535 else np.uint16)
        flat = array.reshape(-1)
        component_type = _component_type_from_dtype(flat.dtype, for_indices=True)
        payload = np.ascontiguousarray(flat).tobytes()
        accessor_type = "SCALAR"
        count = int(flat.shape[0])
    else:
        array = np.asarray(array, dtype=np.float32)
        if array.ndim == 1:
            array = array.reshape(-1, 1)
        accessor_type = _glb_type_from_shape(array)
        component_type = _component_type_from_dtype(array.dtype)
        count = int(array.shape[0])
        payload = np.ascontiguousarray(array).tobytes()

    byte_offset, byte_length = _append_blob(bin_bytes, payload)
    buffer_views = json_doc.setdefault("bufferViews", [])
    buffer_view = {
        "buffer": 0,
        "byteOffset": byte_offset,
        "byteLength": byte_length,
    }
    if target is not None:
        buffer_view["target"] = target
    buffer_view_index = len(buffer_views)
    buffer_views.append(buffer_view)

    accessor = {
        "bufferView": buffer_view_index,
        "componentType": component_type,
        "count": count,
        "type": accessor_type,
    }
    if not for_indices and semantic == "POSITION" and array.ndim == 2 and array.shape[1] == 3:
        accessor["min"] = np.min(array, axis=0).astype(float).tolist()
        accessor["max"] = np.max(array, axis=0).astype(float).tolist()

    accessors = json_doc.setdefault("accessors", [])
    accessor_index = len(accessors)
    accessors.append(accessor)
    return accessor_index


def _decode_draco_primitive(json_doc: dict, bin_bytes: bytearray, primitive: dict) -> None:
    extension = primitive.get("extensions", {}).get("KHR_draco_mesh_compression")
    if not extension:
        return

    buffer_view_index = extension["bufferView"]
    buffer_view = json_doc["bufferViews"][buffer_view_index]
    start = int(buffer_view.get("byteOffset", 0))
    length = int(buffer_view.get("byteLength", 0))
    raw = bytes(bin_bytes[start : start + length])
    mesh = DracoPy.decode(raw)

    attributes = {}
    draco_attrs = extension.get("attributes", {})
    for semantic, unique_id in draco_attrs.items():
        data = None
        semantic_name = semantic

        if semantic == "POSITION":
            data = mesh.points
        elif semantic == "NORMAL":
            data = mesh.normals
        elif semantic.startswith("TEXCOORD_"):
            data = mesh.tex_coord
        elif semantic.startswith("COLOR_"):
            data = mesh.colors
        else:
            attr = mesh.get_attribute_by_unique_id(unique_id)
            if attr is not None:
                data = getattr(attr, "data", None)

        if data is None:
            continue

        target = 34962
        accessor_index = _append_accessor(json_doc, bin_bytes, np.asarray(data), target=target, semantic=semantic_name)
        attributes[semantic_name] = accessor_index

    if getattr(mesh, "faces", None) is not None and len(mesh.faces) > 0:
        indices = np.asarray(mesh.faces, dtype=np.uint32).reshape(-1)
        indices_index = _append_accessor(json_doc, bin_bytes, indices, target=34963, for_indices=True)
        primitive["indices"] = indices_index
    else:
        primitive.pop("indices", None)
        primitive["mode"] = 0

    primitive["attributes"] = attributes
    ext = primitive.get("extensions", {})
    ext.pop("KHR_draco_mesh_compression", None)
    if not ext:
        primitive.pop("extensions", None)
    else:
        primitive["extensions"] = ext


def convert(input_path: str, output_path: str):
    json_doc, bin_bytes = _read_glb(input_path)

    for mesh in json_doc.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            _decode_draco_primitive(json_doc, bin_bytes, primitive)

    for key in ("extensionsRequired", "extensionsUsed"):
        if key in json_doc:
            values = [value for value in json_doc[key] if value != "KHR_draco_mesh_compression"]
            if values:
                json_doc[key] = values
            else:
                json_doc.pop(key, None)

    buffers = json_doc.setdefault("buffers", [])
    if not buffers:
        buffers.append({})
    buffers[0]["byteLength"] = len(bin_bytes)
    buffers[0].pop("uri", None)

    _write_glb(output_path, json_doc, bytes(bin_bytes))


def main():
    if len(sys.argv) != 3:
        print("Usage: python decompress_draco_glb.py <input.glb> <output.glb>", file=sys.stderr)
        raise SystemExit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]
    if not os.path.exists(input_path):
        print(f"Input GLB not found: {input_path}", file=sys.stderr)
        raise SystemExit(1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    convert(input_path, output_path)


if __name__ == "__main__":
    main()
