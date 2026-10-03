"""Compatibility entry point. Prefer `python -m miniagent` or `miniagent`."""

from miniagent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
