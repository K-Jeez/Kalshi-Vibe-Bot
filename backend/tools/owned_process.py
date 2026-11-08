"""Wait on one app process started by dev_control.

The command line includes an owner id. Shutdown uses that id to recognize
this process. Closing the window or stopping this process does not cancel
Kalshi orders.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _title(role: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(f"Kalshi Vibe Bot {role}")
    except Exception:
        return


def _stop_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    proc.terminate()


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("owned process: missing command", file=sys.stderr)
        return 2
    split = argv.index("--")
    option_args = argv[:split]
    command = argv[split + 1 :]
    if not command:
        print("owned process: missing command", file=sys.stderr)
        return 2
    owner = ""
    role = ""
    cwd = None
    index = 0
    while index < len(option_args):
        item = option_args[index]
        if item == "--owner" and index + 1 < len(option_args):
            owner = option_args[index + 1]
            index += 2
            continue
        if item == "--role" and index + 1 < len(option_args):
            role = option_args[index + 1]
            index += 2
            continue
        if item == "--cwd" and index + 1 < len(option_args):
            cwd = option_args[index + 1]
            index += 2
            continue
        index += 1
    if role not in ("backend", "frontend") or not owner:
        print("owned process: owner and role are required", file=sys.stderr)
        return 2
    _title(role)
    print(
        f"Kalshi Vibe Bot {role} window. Closing it stops this process only. "
        "It does not cancel resting Kalshi orders."
    )
    env = os.environ.copy()
    env["VIBE_CONTROL_OWNER"] = owner
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env)
    except OSError:
        print(f"Kalshi Vibe Bot {role} process did not start.", file=sys.stderr)
        return 1

    def _forward(signum, _frame):
        _stop_child(proc)
        raise SystemExit(128 + int(signum))

    import signal

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _forward)
    return int(proc.wait() or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
