"""Offline tests for local process control.

No Kalshi calls, no order routes, and no real Play activation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

import dev_control as control  # noqa: E402
import owned_process  # noqa: E402


SECRET = "sekret-value-should-not-print"
FORBIDDEN_OUTPUT = (SECRET, "BEGIN PRIVATE KEY", "/trade", "cancel-resting", "kalshi.com")


class Fake:
    def __init__(self):
        self.procs = {}
        self.next_pid = 100
        self.killed = []
        self.spawned = []
        self.ports = {}
        self.posts = []
        self.opened = []
        self.calls = []
        self.sleeps = []
        self.attempts = 3
        self.interval = 0
        self.state_queue = []
        self.current = "stop"
        self.health_code = 200
        self.front_code = 200

    def spawn(self, argv, cwd):
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = {
            "cmdline": " ".join(argv),
            "create_token": f"t{pid}",
            "alive": True,
            "argv": list(argv),
            "cwd": str(cwd),
        }
        self.spawned.append(pid)
        return pid

    def inspect(self, pid):
        row = self.procs.get(int(pid))
        if not row or not row["alive"]:
            return None
        return {"pid": int(pid), "cmdline": row["cmdline"], "create_token": row["create_token"]}

    def kill(self, pid):
        self.killed.append(int(pid))
        row = self.procs.get(int(pid))
        if row:
            row["alive"] = False

    def listener(self, port):
        pid = self.ports.get(int(port))
        if pid is None:
            return None
        row = self.procs.get(int(pid))
        if row is not None and not row["alive"]:
            return None
        return int(pid)

    def request(self, method, url, payload, headers):
        self.calls.append((method, url, dict(headers or {})))
        if method == "POST":
            self.posts.append((url, payload, dict(headers)))
            self.current = str(payload["state"])
            return 200, json.dumps({"state": self.current, "api_key": SECRET})
        if url.endswith("/health"):
            body = {"status": "healthy", "version": "2.0.0", "api_key": SECRET}
            if self.health_code != 200:
                body = {"status": "down", "api_key": SECRET}
            return self.health_code, json.dumps(body)
        if url.endswith("/bot/state"):
            if method == "GET" and self.state_queue:
                self.current = self.state_queue.pop(0)
            return 200, json.dumps({"state": self.current, "kalshi_api_key": SECRET, "updated_at": "t"})
        if url.endswith(":3000/"):
            return self.front_code, "<html>SECRET " + SECRET + "</html>"
        return 0, ""

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def open_url(self, url):
        self.opened.append(url)

    def io(self):
        return control.IO(
            spawn=self.spawn,
            inspect=self.inspect,
            kill=self.kill,
            listener=self.listener,
            request=self.request,
            sleep=self.sleep,
            open_url=self.open_url,
            attempts=self.attempts,
            interval=self.interval,
        )


def paths(tmp_path: Path, *, host: str = "127.0.0.1", token: str = "") -> control.Paths:
    root = tmp_path / "VibeBot Project" / "Kalshi Vibe Bot"
    backend = root / "backend"
    frontend = root / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir()
    python_exe = root / "venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    return control.Paths(
        repo_root=root,
        backend_dir=backend,
        frontend_dir=frontend,
        session_path=backend / ".vibe-control" / "session.json",
        python_exe=python_exe,
        wrapper=backend / "tools" / "owned_process.py",
        npm_exe=r"C:\Program Files\nodejs\npm.cmd",
        host=host,
        backend_port=8000,
        frontend_port=3000,
        api_token=token,
    )


def make(tmp_path: Path, **kwargs):
    fake = Fake()
    log = control.Log()
    chosen = paths(tmp_path, **kwargs)
    return control.Control(chosen, fake.io(), log), fake, log, chosen


def text(log: control.Log) -> str:
    return "\n".join(log.lines)


def assert_secret_free(log: control.Log) -> None:
    body = text(log)
    for item in FORBIDDEN_OUTPUT:
        assert item not in body


def test_launch_stays_stop_and_keeps_paths_with_spaces(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    assert app.launch(open_browser=True) == 0
    assert fake.posts == []
    assert len(fake.spawned) == 2
    backend_argv = fake.procs[fake.spawned[0]]["argv"]
    assert str(chosen.python_exe) in backend_argv
    assert backend_argv[backend_argv.index("--cwd") + 1] == str(chosen.backend_dir)
    assert "VibeBot Project" in str(chosen.backend_dir)
    assert fake.opened == ["http://127.0.0.1:3000"]
    assert "Trading state: stop" in text(log)
    assert "Launch does not press Play." in text(log)
    saved = json.loads(chosen.session_path.read_text(encoding="utf-8"))
    assert SECRET not in json.dumps(saved)
    assert_secret_free(log)


def test_frontend_probe_requests_html_and_launch_stays_stop(tmp_path, monkeypatch):
    app, fake, log, _chosen = make(tmp_path)
    assert app.launch() == 0
    frontend = [item for item in fake.calls if item[1].endswith(":3000/")]
    health = [item for item in fake.calls if item[1].endswith("/health")]
    assert frontend and frontend[0][2].get("Accept") == "text/html"
    assert health and "Accept" not in health[0][2]
    assert fake.posts == []
    assert "Trading state: stop" in text(log)

    captured = {}

    class _Response:
        status = 200

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _capture(req, timeout=0):
        captured["accept"] = req.get_header("Accept")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(control.urlrequest, "urlopen", _capture)
    status, _body = control._request(
        "GET",
        "http://127.0.0.1:3000/",
        None,
        {"Accept": "text/html"},
    )
    assert status == 200
    assert captured["accept"] == "text/html"
    control._request("GET", "http://127.0.0.1:8000/health", None, {})
    assert captured["accept"] == "application/json"


def test_second_launch_does_not_spawn_or_change_state(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    assert app.launch() == 0
    fake.state_queue = ["play"]
    fake.spawned.clear()
    fake.posts.clear()
    log.lines.clear()
    assert app.launch() == 0
    assert fake.spawned == []
    assert fake.posts == []
    assert "Trading state: play" in text(log)
    assert "Already running" in text(log)


def test_launch_refuses_unrelated_port_owner(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    fake.procs[50] = {"cmdline": "unrelated-server", "create_token": "other", "alive": True, "argv": []}
    fake.ports[8000] = 50
    assert app.launch() == 1
    assert fake.spawned == []
    assert fake.killed == []
    assert "was not killed" in text(log)
    assert_secret_free(log)


def test_stale_pid_is_not_killed_and_blocks_when_it_holds_the_port(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    fake.procs[50] = {"cmdline": "notepad.exe", "create_token": "other", "alive": True, "argv": []}
    fake.ports[8000] = 50
    control.write_session(
        chosen.session_path,
        {"owner": "owned-token", "processes": [{"role": "backend", "pid": 50, "port": 8000}]},
    )
    assert app.launch() == 1
    assert 50 not in fake.killed
    assert fake.spawned == []
    assert "left running" in text(log)


def test_stale_dead_pid_can_launch_again(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    control.write_session(
        chosen.session_path,
        {
            "owner": "owned-token",
            "processes": [
                {"role": "backend", "pid": 50, "port": 8000},
                {"role": "frontend", "pid": 51, "port": 3000},
            ],
        },
    )
    assert app.launch() == 0
    assert fake.killed == []
    assert len(fake.spawned) == 2
    assert_secret_free(log)


def test_health_failure_stops_only_processes_from_this_launch(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    fake.procs[77] = {"cmdline": "other", "create_token": "x", "alive": True, "argv": []}
    fake.health_code = 500
    assert app.launch() == 1
    assert fake.posts == []
    assert set(fake.killed) == set(fake.spawned)
    assert 77 not in fake.killed
    assert fake.opened == []
    assert "Startup health check failed" in text(log)


def test_launch_corrects_unexpected_play_back_to_stop(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    fake.state_queue = ["play"]
    assert app.launch() == 0
    assert fake.posts[0][0].endswith("/bot/state")
    assert fake.posts[0][1] == {"state": "stop"}
    assert len(fake.posts) == 1
    assert fake.killed == []
    assert_secret_free(log)


def test_launch_stops_itself_when_stop_cannot_be_confirmed(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    fake.state_queue = ["play", "play"]
    assert app.launch() == 1
    assert [item[1] for item in fake.posts] == [{"state": "stop"}]
    assert set(fake.killed) == set(fake.spawned)
    assert "was not in Stop" in text(log)


def test_shutdown_kills_only_matching_owned_pids(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    assert app.launch() == 0
    owned = list(fake.spawned)
    fake.procs[88] = {"cmdline": "python something-else", "create_token": "z", "alive": True, "argv": []}
    fake.ports[8000] = 88
    fake.killed.clear()
    assert app.shutdown() == 0
    assert fake.killed == owned
    assert 88 not in fake.killed
    assert fake.procs[88]["alive"] is True
    assert not chosen.session_path.exists()
    assert control.RISK_PROCESS in text(log)


def test_shutdown_leaves_reused_pid_running(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    control.write_session(
        chosen.session_path,
        {"owner": "owned-token", "processes": [{"role": "backend", "pid": 50, "create_token": "old"}]},
    )
    fake.procs[50] = {"cmdline": "unrelated", "create_token": "new", "alive": True, "argv": []}
    assert app.shutdown() == 0
    assert fake.killed == []
    assert fake.procs[50]["alive"] is True
    assert "left running" in text(log)


def test_repeat_shutdown_is_safe(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    assert app.shutdown() == 0
    assert app.shutdown() == 0
    assert fake.killed == []
    assert "No owned" in text(log)


def test_corrupt_pid_file_kills_nothing(tmp_path):
    app, fake, log, chosen = make(tmp_path)
    chosen.session_path.parent.mkdir(parents=True)
    chosen.session_path.write_text("{not json api_key=" + SECRET, encoding="utf-8")
    assert app.shutdown() == 1
    assert fake.killed == []
    assert SECRET not in text(log)
    assert not chosen.session_path.exists()


def test_restart_does_not_resume_play(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    assert app.launch() == 0
    fake.posts.clear()
    fake.killed.clear()
    before = list(fake.spawned)
    assert app.restart() == 0
    assert fake.killed == before
    assert len(fake.spawned) == 4
    assert fake.posts == []
    assert "Trading state: stop" in text(log)
    assert "Launch does not press Play." in text(log)


def test_trading_commands_use_only_bot_state_and_hide_the_token(tmp_path):
    app, fake, log, _chosen = make(tmp_path, token=SECRET)
    assert app.set_trading("pause") == 0
    assert app.set_trading("stop") == 0
    assert app.set_trading("play") == 0
    assert [item[1]["state"] for item in fake.posts] == ["pause", "stop", "play"]
    assert {item[0] for item in fake.posts} == {"http://127.0.0.1:8000/bot/state"}
    assert all(item[2].get("X-Vibe-Bot-Token") == SECRET for item in fake.posts)
    assert control.RISK_PAUSE in text(log)
    assert control.RISK_STOP in text(log)
    assert_secret_free(log)
    assert fake.spawned == []
    assert fake.killed == []


def test_remote_host_is_refused(tmp_path):
    app, fake, log, _chosen = make(tmp_path, host="192.168.1.20")
    assert app.launch() == 1
    assert app.set_trading("play") == 1
    assert fake.spawned == []
    assert fake.posts == []
    assert "127.0.0.1" in text(log)


def test_status_reports_unowned_server_without_killing_it(tmp_path):
    app, fake, log, _chosen = make(tmp_path)
    fake.procs[77] = {"cmdline": "old-start-bat", "create_token": "old", "alive": True, "argv": []}
    fake.ports[8000] = 77
    assert app.status() == 0
    assert fake.killed == []
    assert fake.posts == []
    assert "did not start it" in text(log)
    assert "Trading state: stop" in text(log)
    assert_secret_free(log)


def test_ambiguous_stop_does_nothing(tmp_path):
    chosen = paths(tmp_path)
    fake = Fake()
    log = control.Log()
    code = control.main(["stop"], paths=chosen, io=fake.io(), log=log)
    assert code == 2
    assert fake.killed == []
    assert fake.posts == []
    assert "shutdown" in text(log)
    assert "bot stop" in text(log)


def test_windows_kill_is_one_pid_tree():
    assert control.windows_kill_argv(4321) == ["taskkill", "/PID", "4321", "/T", "/F"]
    source = (TOOLS / "dev_control.py").read_text(encoding="utf-8")
    wrapper = (TOOLS / "owned_process.py").read_text(encoding="utf-8")
    for name in ("taskkill /IM", "pkill", "killall", "Stop-Process"):
        assert name not in source
        assert name not in wrapper


def test_stop_bat_does_not_kill_by_port_or_image_name():
    stop = (REPO / "stop.bat").read_text(encoding="utf-8").lower()
    start = (REPO / "start.bat").read_text(encoding="utf-8").lower()
    vibe = (REPO / "vibe.bat").read_text(encoding="utf-8")
    assert "netstat" not in stop
    assert "taskkill" not in stop
    assert "vibe.bat" in stop
    assert "vibe.bat" in start
    assert "dev_control.py" in vibe
    doc = (REPO / "DEV_CONTROL.md").read_text(encoding="utf-8")
    assert control.RISK_PAUSE in doc
    assert control.RISK_STOP in doc
    assert control.RISK_PROCESS in doc


def test_identity_requires_owner_role_and_create_token():
    record = {"owner": "abc", "role": "backend", "pid": 5, "create_token": "t1"}
    live = {"pid": 5, "cmdline": "python owned_process.py --owner abc --role backend -- run.py", "create_token": "t1"}
    assert control.matches_identity(record, live)
    quoted = {**live, "cmdline": '"python" --owner "abc" --role backend -- run.py'}
    assert control.matches_identity(record, quoted)
    assert not control.matches_identity(record, {**live, "create_token": "t2"})
    assert not control.matches_identity(record, {**live, "cmdline": "python --owner abc --role frontend"})
    assert not control.matches_identity(record, None)


def test_real_request_posts_bot_state_and_status_hides_extra_fields():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["body"] = self.rfile.read(length)
            seen["token"] = self.headers.get("X-Vibe-Bot-Token")
            raw = json.dumps({"state": "stop", "api_key": SECRET}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = control._request(
            "POST",
            f"http://127.0.0.1:{port}/bot/state",
            {"state": "stop"},
            {"X-Vibe-Bot-Token": SECRET},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert seen["path"] == "/bot/state"
    assert json.loads(seen["body"]) == {"state": "stop"}
    assert seen["token"] == SECRET
    shown = control.scrub(body)
    assert SECRET not in shown
    assert "api_key=[redacted]" in shown


def test_real_shutdown_kills_only_the_owned_process(tmp_path):
    owner = "11111111-1111-1111-1111-111111111111"
    argv = [
        sys.executable,
        str(TOOLS / "owned_process.py"),
        "--owner",
        owner,
        "--role",
        "backend",
        "--cwd",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    ]
    pid = control._spawn(argv, tmp_path)
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        live = control._inspect(pid)
        record = {"owner": owner, "role": "backend", "pid": pid, "create_token": live["create_token"]}
        assert control.matches_identity(record, live)
        assert not control.matches_identity(
            {**record, "pid": bystander.pid, "owner": "other"},
            control._inspect(bystander.pid),
        )
        chosen = paths(tmp_path)
        control.write_session(
            chosen.session_path,
            {"owner": owner, "processes": [record]},
        )
        fake = Fake()
        log = control.Log()
        io = control.IO(
            spawn=fake.spawn,
            inspect=control._inspect,
            kill=control._kill,
            listener=lambda _port: None,
            request=fake.request,
            sleep=fake.sleep,
            open_url=fake.open_url,
            attempts=1,
            interval=0,
        )
        assert control.Control(chosen, io, log).shutdown() == 0
        import time

        for _ in range(40):
            if control._inspect(pid) is None:
                break
            time.sleep(0.05)
        assert control._inspect(pid) is None
        assert bystander.poll() is None
        assert fake.posts == []
    finally:
        if control._inspect(pid) is not None:
            control._kill(pid)
        if bystander.poll() is None:
            bystander.kill()
            bystander.wait(timeout=5)


def test_owned_process_rejects_a_missing_command():
    assert owned_process.main(["--owner", "abc", "--role", "backend"]) == 2


@pytest.mark.parametrize("host", ["", "localhost", "0.0.0.0", "::1", "127.0.0.1"])
def test_normalize_host_stays_on_loopback(host):
    assert control.normalize_host(host) == "127.0.0.1"


def test_normalize_host_rejects_a_remote_address():
    with pytest.raises(ValueError):
        control.normalize_host("10.0.0.8")
