"""macOS caffeinate helper — prevents idle sleep during long-running commands."""
from __future__ import annotations

import contextlib
import subprocess


@contextlib.contextmanager
def caffeinate():
    """Run `caffeinate -i` for the duration of the block, then terminate it."""
    proc = subprocess.Popen(["caffeinate", "-i"], close_fds=True)
    try:
        yield
    finally:
        proc.terminate()
        proc.wait()
