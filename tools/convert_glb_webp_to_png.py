import base64
import json
import os
import struct
import sys
from io import BytesIO

from PIL import Image


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

    return json_doc, bin_bytes


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


def convert(input_path: str, output_path: str):
    json_doc, bin_bytes = _read_glb(input_path)
    buffer_views = json_doc.get("bufferViews", [])
    images = json_doc.get("images", [])

    replacements = {}
    for image_index, image in enumerate(images):
        if image.get("mimeType") != "image/webp":
            continue
        buffer_view_index = image.get("bufferView")
        if buffer_view_index is None:
            continue
        if buffer_view_index < 0 or buffer_view_index >= len(buffer_views):
            continue

        view = buffer_views[buffer_view_index]
        start = int(view.get("byteOffset", 0))
        length = int(view.get("byteLength", 0))
        raw = bin_bytes[start : start + length]
        pil = Image.open(BytesIO(raw))
        out = BytesIO()
        if pil.mode not in ("RGB", "RGBA", "LA"):
            pil = pil.convert("RGBA" if "A" in pil.getbands() else "RGB")
        pil.save(out, format="PNG", optimize=True)
        replacements[buffer_view_index] = out.getvalue()
        image["mimeType"] = "image/png"

    # Clean up EXT_texture_webp from textures
    textures = json_doc.get("textures", [])
    for texture in textures:
        exts = texture.get("extensions", {})
        if "EXT_texture_webp" in exts:
            webp_source = exts["EXT_texture_webp"].get("source")
            if webp_source is not None:
                texture["source"] = webp_source
            exts.pop("EXT_texture_webp")
            if not exts:
                texture.pop("extensions")

    # Clean up extensionsRequired and extensionsUsed
    for key in ("extensionsRequired", "extensionsUsed"):
        if key in json_doc:
            values = [value for value in json_doc[key] if value != "EXT_texture_webp"]
            if values:
                json_doc[key] = values
            else:
                json_doc.pop(key, None)

    if not replacements:
        # Nothing to change, but still emit a clean copy.
        _write_glb(output_path, json_doc, bin_bytes)
        return

    new_bin = bytearray()
    for i, view in enumerate(buffer_views):
        start = int(view.get("byteOffset", 0))
        length = int(view.get("byteLength", 0))
        chunk = replacements.get(i, bin_bytes[start : start + length])

        while len(new_bin) % 4 != 0:
            new_bin.append(0)

        view["byteOffset"] = len(new_bin)
        view["byteLength"] = len(chunk)
        new_bin.extend(chunk)

    buffers = json_doc.get("buffers", [])
    if buffers:
        buffers[0]["byteLength"] = len(new_bin)

    _write_glb(output_path, json_doc, bytes(new_bin))


def main():
    if len(sys.argv) != 3:
        print("Usage: python convert_glb_webp_to_png.py <input.glb> <output.glb>", file=sys.stderr)
        raise SystemExit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]
    if not os.path.exists(input_path):
        print(f"Input GLB not found: {input_path}", file=sys.stderr)
        raise SystemExit(1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    convert(input_path, output_path)


if __name__ == "__main__":
    main()
