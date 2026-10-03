"""Build only the fixed context needed at startup; files are read on demand."""

import platform
from pathlib import Path

from .processes import shell_description


def fixed_messages(workspace):
    workspace = Path(workspace).resolve()
    instructions = Path(__file__).with_name("instructions.md").read_text(encoding="utf-8")
    shell = shell_description()
    environment = (
        f"Workspace: {workspace}\nOS: {platform.platform()}\nShell: {shell}\n"
        "Paths for file tools and command cwd are relative to the workspace. "
        "Shell commands are not sandboxed. Background jobs belong to this process only."
    )
    messages = [{"role": "system", "content": instructions + "\n\n" + environment}]
    for filename in ("AGENTS.md", "Agent.md"):
        path = workspace / filename
        if path.is_file():
            if path.resolve().parent != workspace:
                raise ValueError(f"Project instructions must remain inside workspace: {filename}")
            if path.stat().st_size > 64_000:
                raise ValueError(f"{filename} exceeds 64 KB; shorten project instructions.")
            messages.append({"role": "system", "content": f"Project instructions ({filename}):\n"
                             + path.read_text(encoding="utf-8-sig")})
            break
    return messages
