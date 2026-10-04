"""Install terminal dependencies using the release builder's verified cache."""

import subprocess
import sys

from build_release import DEPENDENCIES, WHEEL_CACHE, _wheel, _wheel_files


def main():
    wheels = []
    for name, version in DEPENDENCIES.items():
        data = _wheel(name, version, None)
        _wheel_files(data, name, version)
        wheels.append(str(WHEEL_CACHE / f"{name}-{version}-py3-none-any.whl"))
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-cache-dir", *wheels],
        check=True,
    )


if __name__ == "__main__":
    main()
