#!/usr/bin/env python3
"""Windows and local process control for the Kalshi Vibe Bot dev app.

Process commands start and stop the backend and frontend this tool launched.
Trading commands only call the same local ``/bot/state`` route as the
dashboard. Launch and restart do not press Play.

This tool does not place orders, cancel orders, or call Kalshi.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

RISK_PAUSE = (
    "Pause stops new buys. If stop-loss selling is enabled in Settings, Pause can still sell."
)
RISK_STOP = (
    "Stop stops new buys and turns off automatic exits. "
    "Stop does not cancel resting orders at Kalshi."
)
RISK_PROCESS = (
    "Closing the backend process does not cancel resting orders at Kalshi. "
    "The next launch comes up in Stop and does not press Play."
)

_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|private[_-]?key|token|secret|password|authorization|bearer)\b)"
    r"\s*[\"']?\s*[:=]\s*[\"']?[^\s,\"'}]+"
)
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def scrub(text: str) -> str:
    """Remove secret-shaped text from a line before it is shown."""
    cleaned = _PEM_RE.sub("[redacted]", text or "")
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)


def windows_kill_argv(pid: int) -> list[str]:
    """Kill one process tree by pid. This is not a name-wide python or node kill."""
    return ["taskkill", "/PID", str(int(pid)), "/T", "/F"]


def normalize_host(host: str) -> str:
    """Map a local bind address to the loopback address this control may call."""
    value = (host or "").strip().lower()
    if value in ("", "0.0.0.0", "::", "::1", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    raise ValueError("remote")


def matches_identity(record: dict, live: Optional[dict]) -> bool:
    """True when the live process is the one recorded, not a reused pid."""
    if not live or not record:
        return False
    owner = str(record.get("owner") or "")
    role = str(record.get("role") or "")
    if not owner or role not in ("backend", "frontend"):
        return False
    parts = [part.strip('"').strip("'") for part in str(live.get("cmdline") or "").split()]
    if owner not in parts:
        return False
    found_role = False
    for index, part in enumerate(parts):
        if part == "--role" and index + 1 < len(parts) and parts[index + 1] == role:
            found_role = True
            break
    if not found_role:
        return False
    wanted = str(record.get("create_token") or "")
    found = str(live.get("create_token") or "")
    if wanted and found and wanted != found:
        return False
    return int(live.get("pid") or 0) == int(record.get("pid") or -1)


def resolve_python(repo: Path) -> Path:
    candidates = (
        repo / "venv" / "Scripts" / "python.exe",
        repo / ".venv" / "Scripts" / "python.exe",
        repo / "venv" / "bin" / "python",
        repo / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


@dataclass
class Paths:
    repo_root: Path
    backend_dir: Path
    frontend_dir: Path
    session_path: Path
    python_exe: Path
    wrapper: Path
    npm_exe: str
    host: str
    backend_port: int
    frontend_port: int
    api_token: str = ""


@dataclass
class Log:
    lines: list[str] = field(default_factory=list)

    def say(self, message: str) -> None:
        self.lines.append(scrub(message))


@dataclass
class IO:
    spawn: Callable[[list[str], Path], int]
    inspect: Callable[[int], Optional[dict]]
    kill: Callable[[int], None]
    listener: Callable[[int], Optional[int]]
    request: Callable[[str, str, Optional[dict], dict], tuple[int, str]]
    sleep: Callable[[float], None]
    open_url: Callable[[str], None]
    attempts: int = 40
    interval: float = 1.0


def load_session(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"corrupt": True}
    if not isinstance(data, dict) or not isinstance(data.get("processes"), list):
        return {"corrupt": True}
    return data


def write_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def quarantine_session(path: Path) -> None:
    if not path.is_file():
        return
    dest = path.with_name(path.name + ".unreadable")
    if dest.exists():
        dest.unlink()
    path.replace(dest)


def clear_session(path: Path) -> None:
    if path.is_file():
        path.unlink()


class Control:
    def __init__(self, paths: Paths, io: IO, log: Log):
        self.paths = paths
        self.io = io
        self.log = log

    def launch(self, *, open_browser: bool = False) -> int:
        if not self._local():
            return 1
        existing = self._prepare_existing()
        if existing == "running":
            self._report_trading(allow_unowned=True)
            self.log.say("Already running. Launch did not change Play, Pause, or Stop.")
            return 0
        if existing == "blocked":
            return 1
        occupant = self._foreign_listener()
        if occupant is not None:
            self.log.say(
                f"Port {occupant[0]} is already used by pid {occupant[1]}. "
                "That process was not started by this control, so it was not killed."
            )
            return 1
        if not self.paths.npm_exe:
            self.log.say("npm was not found. Nothing was started.")
            return 1
        owner = str(uuid.uuid4())
        spawned: list[dict] = []
        try:
            for role, argv, cwd, port in self._commands(owner):
                pid = int(self.io.spawn(list(argv), cwd))
                live = self.io.inspect(pid) or {}
                spawned.append(
                    {
                        "role": role,
                        "pid": pid,
                        "owner": owner,
                        "create_token": str(live.get("create_token") or ""),
                        "port": port,
                    }
                )
        except OSError:
            self.log.say("A process did not start. Owned processes from this launch were stopped.")
            self._kill_records(spawned)
            return 1
        try:
            write_session(
                self.paths.session_path,
                {"owner": owner, "processes": spawned},
            )
        except OSError:
            self.log.say("The pid file could not be saved. Owned processes from this launch were stopped.")
            self._kill_records(spawned)
            return 1
        if not self._wait_local_health():
            self.log.say("Startup health check failed. Owned processes from this launch were stopped.")
            self.log.say(RISK_PROCESS)
            self._kill_records(spawned)
            clear_session(self.paths.session_path)
            return 1
        state = self._read_state()
        if state != "stop":
            self._post_state("stop")
            state = self._read_state()
        if state != "stop":
            self.log.say("The new backend was not in Stop. Owned processes from this launch were stopped.")
            self._kill_records(spawned)
            clear_session(self.paths.session_path)
            return 1
        self.log.say("Process: backend and frontend were started.")
        self.log.say("Trading state: stop")
        self.log.say("Launch does not press Play.")
        self.log.say(RISK_PROCESS)
        if open_browser:
            self.io.open_url("http://127.0.0.1:3000")
        return 0

    def shutdown(self) -> int:
        session = load_session(self.paths.session_path)
        if session is None:
            self.log.say("No owned Kalshi Vibe Bot processes.")
            self.log.say(RISK_PROCESS)
            return 0
        if session.get("corrupt"):
            quarantine_session(self.paths.session_path)
            self.log.say("The pid file could not be read. No process was killed.")
            self.log.say(RISK_PROCESS)
            return 1
        records = self._records(session)
        killed = 0
        for record in records:
            live = self.io.inspect(int(record["pid"]))
            if live is None:
                self.log.say(f"Process {record['role']}: already stopped.")
                continue
            if not matches_identity(record, live):
                self.log.say(
                    f"Pid {record['pid']} does not match the owned {record['role']} process. "
                    "It was left running."
                )
                continue
            self.io.kill(int(record["pid"]))
            killed += 1
            self.log.say(f"Process {record['role']}: stopped pid {record['pid']}.")
        clear_session(self.paths.session_path)
        if killed == 0 and records:
            self.log.say("No owned process was stopped.")
        self.log.say(RISK_PROCESS)
        return 0

    def restart(self, *, open_browser: bool = False) -> int:
        code = self.shutdown()
        if code != 0:
            self.log.say("Restart stopped because shutdown could not verify its pid file.")
            return code
        return self.launch(open_browser=open_browser)

    def status(self) -> int:
        if not self._local():
            return 1
        session = load_session(self.paths.session_path)
        owned = []
        if session and not session.get("corrupt"):
            owned = self._records(session)
        seen = False
        for record in owned:
            live = self.io.inspect(int(record["pid"]))
            if matches_identity(record, live):
                seen = True
                self.log.say(f"Process {record['role']}: running pid {record['pid']}")
            elif live is None:
                self.log.say(f"Process {record['role']}: recorded pid {record['pid']} is not running")
            else:
                self.log.say(
                    f"Process {record['role']}: recorded pid {record['pid']} is a different process and was left running"
                )
        if not seen:
            self._report_unowned_listener()
        self._report_trading(allow_unowned=True)
        self.log.say("Process status is separate from trading state.")
        self.log.say(RISK_STOP)
        self.log.say(RISK_PROCESS)
        return 0

    def set_trading(self, state: str) -> int:
        if state not in ("play", "pause", "stop"):
            self.log.say("Trading state must be play, pause, or stop.")
            return 2
        if not self._local():
            return 1
        code = self._post_state(state)
        if code != 200:
            self.log.say("Trading state was not changed.")
            return 1
        confirmed = self._read_state()
        if confirmed != state:
            self.log.say("Trading state was not confirmed.")
            return 1
        self.log.say(f"Trading state: {state}")
        if state == "play":
            self.log.say("Play can scan and place real orders. Strategy settings were not changed.")
        elif state == "pause":
            self.log.say(RISK_PAUSE)
        else:
            self.log.say(RISK_STOP)
        return 0

    def _local(self) -> bool:
        if self.paths.host != "127.0.0.1":
            self.log.say("This control only talks to 127.0.0.1. Nothing was changed.")
            return False
        return True

    def _prepare_existing(self) -> str:
        session = load_session(self.paths.session_path)
        if session is None:
            return "clear"
        if session.get("corrupt"):
            quarantine_session(self.paths.session_path)
            self.log.say("The pid file could not be read. It was set aside and nothing was killed.")
            return "clear"
        records = self._records(session)
        live_roles = []
        stale = False
        for record in records:
            live = self.io.inspect(int(record["pid"]))
            if live is None:
                stale = True
                continue
            if matches_identity(record, live):
                live_roles.append(record["role"])
                continue
            stale = True
            self.log.say(
                f"Pid {record['pid']} is not the owned {record['role']} process. It was left running."
            )
        if set(live_roles) == {"backend", "frontend"}:
            return "running"
        if live_roles:
            self.log.say("An owned process is already running. Run shutdown before launch. Nothing new was started.")
            return "blocked"
        if stale:
            clear_session(self.paths.session_path)
        return "clear"

    def _records(self, session: dict) -> list[dict]:
        owner = str(session.get("owner") or "")
        rows = []
        for item in session.get("processes") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in ("backend", "frontend"):
                continue
            try:
                pid = int(item.get("pid"))
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "role": role,
                    "pid": pid,
                    "owner": str(item.get("owner") or owner),
                    "create_token": str(item.get("create_token") or ""),
                    "port": item.get("port"),
                }
            )
        return rows

    def _foreign_listener(self) -> Optional[tuple[int, int]]:
        owned = set()
        session = load_session(self.paths.session_path)
        if session and not session.get("corrupt"):
            owned = {int(item["pid"]) for item in self._records(session)}
        for port in (self.paths.backend_port, self.paths.frontend_port):
            listener = self.io.listener(int(port))
            if listener is None or int(listener) in owned:
                continue
            return int(port), int(listener)
        return None

    def _commands(self, owner: str):
        backend = [
            str(self.paths.python_exe),
            str(self.paths.wrapper),
            "--owner",
            owner,
            "--role",
            "backend",
            "--cwd",
            str(self.paths.backend_dir),
            "--",
            str(self.paths.python_exe),
            "run.py",
        ]
        frontend = [
            str(self.paths.python_exe),
            str(self.paths.wrapper),
            "--owner",
            owner,
            "--role",
            "frontend",
            "--cwd",
            str(self.paths.frontend_dir),
            "--",
            self.paths.npm_exe,
            "run",
            "dev",
            "--",
            "--port",
            str(self.paths.frontend_port),
            "--strictPort",
            "--host",
            "127.0.0.1",
        ]
        return (
            ("backend", backend, self.paths.backend_dir, self.paths.backend_port),
            ("frontend", frontend, self.paths.frontend_dir, self.paths.frontend_port),
        )

    def _kill_records(self, records: list[dict]) -> None:
        for record in records:
            live = self.io.inspect(int(record["pid"]))
            if matches_identity(record, live):
                self.io.kill(int(record["pid"]))

    def _headers(self) -> dict:
        token = (self.paths.api_token or "").strip()
        if not token:
            return {}
        return {"X-Vibe-Bot-Token": token}

    def _request(self, method: str, path: str, payload: Optional[dict]) -> tuple[int, str]:
        url = f"http://{self.paths.host}:{self.paths.backend_port}{path}"
        try:
            return self.io.request(method, url, payload, self._headers())
        except Exception:
            return 0, ""

    def _wait_local_health(self) -> bool:
        for attempt in range(self.io.attempts):
            status, text = self._request("GET", "/health", None)
            data = _json_object(text)
            version = str(data.get("version") or "")
            front_status = 0
            if status == 200 and data.get("status") == "healthy":
                try:
                    front_status, _front_text = self.io.request(
                        "GET",
                        f"http://{self.paths.host}:{self.paths.frontend_port}/",
                        None,
                        {},
                    )
                except Exception:
                    front_status = 0
            if status == 200 and data.get("status") == "healthy" and int(front_status) == 200:
                if _VERSION_RE.fullmatch(version):
                    self.log.say(f"Health: healthy version {version}")
                else:
                    self.log.say("Health: healthy")
                return True
            if attempt + 1 < self.io.attempts:
                self.io.sleep(self.io.interval)
        return False

    def _read_state(self) -> str:
        status, text = self._request("GET", "/bot/state", None)
        if status != 200:
            return ""
        state = str(_json_object(text).get("state") or "")
        if state in ("play", "pause", "stop"):
            return state
        return ""

    def _post_state(self, state: str) -> int:
        status, _text = self._request("POST", "/bot/state", {"state": state})
        return int(status)

    def _report_trading(self, *, allow_unowned: bool) -> None:
        del allow_unowned
        state = self._read_state()
        if state:
            self.log.say(f"Trading state: {state}")
            return
        self.log.say("Trading state: unavailable")

    def _report_unowned_listener(self) -> None:
        answered = self._request("GET", "/health", None)[0] == 200
        listener = self.io.listener(int(self.paths.backend_port))
        if answered or listener is not None:
            if listener is not None:
                self.log.say(
                    f"Process backend: pid {int(listener)} is answering on port {self.paths.backend_port} "
                    "but this control did not start it. Shutdown will not stop that pid."
                )
            else:
                self.log.say(
                    f"Process backend: a server is answering on port {self.paths.backend_port} "
                    "but this control did not start it. Shutdown will not stop it."
                )
            return
        self.log.say("Process: not running under this control")


def _json_object(text: str) -> dict:
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  vibe.bat launch",
            "  vibe.bat shutdown",
            "  vibe.bat restart",
            "  vibe.bat status",
            "  vibe.bat bot play",
            "  vibe.bat bot pause",
            "  vibe.bat bot stop",
            "",
            "launch, shutdown, and restart change processes. They do not press Play.",
            "bot play, bot pause, and bot stop change trading state only.",
            "shutdown is not bot stop. Neither command cancels resting Kalshi orders.",
            RISK_PAUSE,
            RISK_STOP,
            RISK_PROCESS,
        ]
    )


def main(argv: list[str], *, paths: Optional[Paths] = None, io: Optional[IO] = None, log: Optional[Log] = None) -> int:
    log = log or Log()
    command = list(argv)
    if not command or command[0] in ("-h", "--help", "help"):
        log.say(usage())
        _emit(log)
        return 0 if command else 2
    if command == ["stop"]:
        log.say("stop is ambiguous. Use shutdown for the process, or bot stop for trading state.")
        log.say("Neither command cancels resting Kalshi orders.")
        _emit(log)
        return 2
    paths = paths or load_paths(Path(__file__).resolve().parents[2])
    io = io or real_io()
    if paths is None:
        log.say("Backend settings could not be read. Nothing was started or changed.")
        _emit(log)
        return 1
    control = Control(paths, io, log)
    open_browser = "--open-browser" in command
    command = [item for item in command if item != "--open-browser"]
    if command == ["launch"]:
        code = control.launch(open_browser=open_browser)
    elif command == ["shutdown"]:
        code = control.shutdown()
    elif command == ["restart"]:
        code = control.restart(open_browser=open_browser)
    elif command == ["status"]:
        code = control.status()
    elif command == ["bot", "play"]:
        code = control.set_trading("play")
    elif command == ["bot", "pause"]:
        code = control.set_trading("pause")
    elif command == ["bot", "stop"]:
        code = control.set_trading("stop")
    else:
        log.say(usage())
        code = 2
    _emit(log)
    return code


def _emit(log: Log) -> None:
    for line in log.lines:
        print(scrub(line))


def load_paths(repo: Path) -> Optional[Paths]:
    backend = repo / "backend"
    sys.path.insert(0, str(backend))
    try:
        from src.config import settings

        host = normalize_host(str(getattr(settings, "host", "") or ""))
        port = int(getattr(settings, "port", 8000))
        token = str(getattr(settings, "local_api_token", "") or "")
    except Exception:
        return None
    npm = shutil.which("npm") or ""
    return Paths(
        repo_root=repo,
        backend_dir=backend,
        frontend_dir=repo / "frontend",
        session_path=backend / ".vibe-control" / "session.json",
        python_exe=resolve_python(repo),
        wrapper=backend / "tools" / "owned_process.py",
        npm_exe=npm,
        host=host,
        backend_port=port,
        frontend_port=3000,
        api_token=token,
    )


def real_io() -> IO:
    return IO(
        spawn=_spawn,
        inspect=_inspect,
        kill=_kill,
        listener=_listener,
        request=_request,
        sleep=lambda seconds: __import__("time").sleep(seconds),
        open_url=_open_url,
    )


def _spawn(argv: list[str], cwd: Path) -> int:
    kwargs = {"cwd": str(cwd), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(list(argv), **kwargs)
    pid = int(proc.pid)
    # Drop the handle without waiting. A dev server must keep running after launch returns.
    proc.returncode = 0
    return pid


def _inspect(pid: int) -> Optional[dict]:
    if os.name == "nt":
        return _inspect_windows(pid)
    return _inspect_linux(pid)


def _inspect_linux(pid: int) -> Optional[dict]:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    stat_path = Path(f"/proc/{pid}/stat")
    if not cmdline_path.exists():
        return None
    command = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    create = ""
    try:
        stat = stat_path.read_text(encoding="utf-8")
        tail = stat.rsplit(")", 1)[1].split()
        create = tail[19]
    except (OSError, IndexError):
        create = ""
    return {"pid": int(pid), "cmdline": command, "create_token": create}


def _inspect_windows(pid: int) -> Optional[dict]:
    script = (
        f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\"; "
        "if (-not $p) { exit 3 }; "
        "Write-Output $p.CommandLine; "
        "Write-Output $p.CreationDate.ToUniversalTime().ToString('o')"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    create = lines[-1] if len(lines) > 1 else ""
    command = lines[0] if len(lines) == 1 else " ".join(lines[:-1])
    return {"pid": int(pid), "cmdline": command, "create_token": create}


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(windows_kill_argv(pid), check=False, capture_output=True)
        return
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    import time

    time.sleep(0.2)
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except ChildProcessError:
        return


def _listener(port: int) -> Optional[int]:
    if os.name == "nt":
        return _listener_windows(port)
    return _listener_linux(port)


def _listener_windows(port: int) -> Optional[int]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        check=False,
        capture_output=True,
        text=True,
    )
    pattern = re.compile(rf":{int(port)}(?!\d)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)
    for line in completed.stdout.splitlines():
        match = pattern.search(line.strip())
        if match:
            return int(match.group(1))
    return None


def _listener_linux(port: int) -> Optional[int]:
    hex_port = f"{int(port):04X}"
    inodes = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local = fields[1]
            state = fields[3]
            if not local.endswith(":" + hex_port) or state != "0A":
                continue
            inodes.add(fields[9])
    if not inodes:
        return None
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            for inode in inodes:
                if target == f"socket:[{inode}]":
                    return int(entry.name)
    return None


def _request(method: str, url: str, payload: Optional[dict], headers: dict) -> tuple[int, str]:
    data = None
    req_headers = {"Accept": "application/json"}
    req_headers.update(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=3) as response:
            body = response.read().decode("utf-8", "replace")
            return int(response.status), body
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return int(exc.code), body
    except Exception:
        return 0, ""


def _open_url(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
