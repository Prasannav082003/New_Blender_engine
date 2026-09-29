"""
MCP server exposing the Godot renderer as tools.

Attach any MCP client - Claude Desktop, an IDE, your own - and drive the
scene by hand. It is built from the SAME registry in tools.py that the
internal Qwen agent uses, so there is exactly one implementation of
"add a light" and it cannot drift between the two paths.

Run it:

    # stdio (Claude Desktop)
    python -m zlendo_llm.mcp_server --scene image.json

    # HTTP (network clients)
    python -m zlendo_llm.mcp_server --scene image.json --transport streamable-http --port 8765

Claude Desktop config:

    {
      "mcpServers": {
        "godot-render": {
          "command": "C:\\\\Zlendo2026\\\\4k_PT_Engine_dev\\\\venv\\\\Scripts\\\\python.exe",
          "args": ["-m", "zlendo_llm.mcp_server", "--scene", "image.json"],
          "cwd": "C:\\\\Zlendo2026\\\\4k_PT_Engine_dev"
        }
      }
    }
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .session import RenderSession, SessionConfig
from .tools import TOOLS, dispatch, to_text

log = logging.getLogger(__name__)

# Module-level session: an MCP server process serves one scene at a time.
_SESSION: Optional[RenderSession] = None


def _session() -> RenderSession:
    if _SESSION is None:
        raise RuntimeError("No scene loaded. Start the server with --scene.")
    return _SESSION


def build_server(session: RenderSession, name: str = "godot-render"):
    """
    Wrap the registry in a FastMCP server.

    Tools are registered dynamically from TOOLS rather than written out
    with decorators, so adding a tool to the registry automatically
    exposes it over MCP with no extra code.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The MCP SDK is not installed. Run:  pip install mcp"
        ) from exc

    mcp = FastMCP(name)

    for tool in TOOLS:
        _register(mcp, tool, session)

    # Expose the live scene state as a readable resource too - clients can
    # inspect it without burning a tool call.
    @mcp.resource("scene://state")
    def scene_state() -> str:
        """Current lighting settings, added lights, and render history."""
        return json.dumps(session.summary(), indent=2, default=str)

    @mcp.resource("scene://analysis")
    def scene_analysis() -> str:
        """The most recent scene analysis, if one has been run."""
        return json.dumps(session.audit or {"note": "run analyze_scene first"},
                          indent=2, default=str)

    return mcp


_JSON_TO_PY = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _register(mcp, tool, session: RenderSession) -> None:
    """
    Register one registry Tool with FastMCP.

    FastMCP derives its schema from the function signature via pydantic,
    and - importantly - it also VALIDATES incoming calls against that same
    derived model. So simply attaching our JSON Schema to the registered
    tool is not enough: the client would see the right schema but every
    call would fail validation against the underlying `**kwargs`.

    Instead we synthesise a real signature from the registry's JSON Schema.
    Required fields become required keyword-only params; optional ones get
    `Optional[T] = None`. FastMCP then infers a correct schema AND accepts
    the calls. One schema definition, no drift.
    """
    props: Dict[str, Any] = tool.parameters.get("properties", {}) or {}
    required = set(tool.parameters.get("required", []) or [])

    params = []
    annotations: Dict[str, Any] = {}
    for key, spec in props.items():
        py_type = _JSON_TO_PY.get(spec.get("type", "string"), str)
        if key in required:
            params.append(
                inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=py_type)
            )
            annotations[key] = py_type
        else:
            params.append(
                inspect.Parameter(
                    key,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=Optional[py_type],
                    default=None,
                )
            )
            annotations[key] = Optional[py_type]

    def _handler(**kwargs: Any) -> str:
        # Strip unset optionals so tool handlers see absence, not None.
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        return to_text(dispatch(session, tool.name, cleaned))

    _handler.__name__ = tool.name
    _handler.__doc__ = tool.description
    _handler.__signature__ = inspect.Signature(params, return_annotation=str)
    annotations["return"] = str
    _handler.__annotations__ = annotations

    try:
        mcp.add_tool(_handler, name=tool.name, description=tool.description)
    except Exception:  # pragma: no cover - SDK surface varies by version
        log.exception("Could not register tool %s", tool.name)


def load_session(
    scene_path: str,
    godot_exe: Optional[str] = None,
    project_dir: Optional[str] = None,
    max_renders: int = 3,
    job_id: str = "mcp",
) -> RenderSession:
    scene_file = Path(scene_path)
    scene = json.loads(scene_file.read_text(encoding="utf-8"))

    godot = godot_exe or os.getenv("GODOT_EXE")
    project = project_dir or os.getenv(
        "GODOT_PROJECT_PATH", str(Path.cwd() / "godot_project")
    )
    if not godot:
        raise SystemExit(
            "Set --godot or the GODOT_EXE environment variable to the Godot binary."
        )

    cfg = SessionConfig(
        godot_exe=godot,
        project_dir=project,
        max_renders=max_renders,
    )
    return RenderSession(scene, cfg, job_id=job_id)


def main() -> int:
    # stderr, never stdout: stdout is the MCP transport under stdio and
    # any stray print corrupts the protocol stream.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(description="MCP server for the Godot 4K renderer.")
    ap.add_argument("--scene", required=True, help="Scene JSON to load.")
    ap.add_argument("--godot", default=None, help="Godot binary (or set GODOT_EXE).")
    ap.add_argument("--project", default=None, help="godot_project dir.")
    ap.add_argument("--max-renders", type=int, default=3)
    ap.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
    )
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    global _SESSION
    _SESSION = load_session(
        args.scene, args.godot, args.project, args.max_renders
    )

    server = build_server(_SESSION)

    if args.transport != "stdio":
        server.settings.port = args.port
        server.settings.host = "127.0.0.1"

    log.info("Serving %d tools over %s", len(TOOLS), args.transport)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
