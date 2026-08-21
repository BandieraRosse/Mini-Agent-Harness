"""Local tools. Their return values are the only source of execution truth."""

import json
from pathlib import Path


def read_file(path):
    try:
        return {"ok": True, "path": path, "content": Path(path).read_text(encoding="utf-8")}
    except Exception as error:
        return {"ok": False, "path": path, "error": f"{type(error).__name__}: {error}"}


TOOLS = {"read_file": read_file}
TOOL_SCHEMAS = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
