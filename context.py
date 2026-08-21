"""Loads the fixed context and builds the mutable conversation input."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).parent
INSTRUCTIONS_PATH = PROJECT_ROOT / "INSTRUCTIONS.md"
AGENTS_PATH = PROJECT_ROOT / "AGENTS.md"


def load_fixed_context():
    return {
        "instructions": INSTRUCTIONS_PATH.read_text(encoding="utf-8"),
        "agents": AGENTS_PATH.read_text(encoding="utf-8"),
    }


def new_context(user_input, agents):
    return {
        "world_state": {"cwd": str(Path.cwd())},
        "input": [
            {"role": "system", "content": "Project context from AGENTS.md:\n" + agents},
            {"role": "user", "content": user_input},
        ],
    }
