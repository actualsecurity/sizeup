import os
import socket
import sys
import time

from sizeup.app import WhisperService


def fake_server(tmp_path):
    """A stand-in named whisper-server that just listens on --port."""
    exe = tmp_path / "whisper-server"
    exe.write_text(f"#!{sys.executable}\n"
                   "import socket, sys, time\n"
                   "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
                   "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                   "s.bind(('127.0.0.1', port)); s.listen(); time.sleep(120)\n")
    exe.chmod(0o755)
    return str(exe)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(cond, s=10.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.05)
    return cond()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_own_server_replaced_when_config_changes_but_kept_when_same(tmp_path):
    exe, port, state = fake_server(tmp_path), free_port(), tmp_path / "whisper-server.json"
    a = WhisperService(exe, "turbo.bin", port, 8, state_path=state).start()
    assert wait(lambda: a.state == "ok")
    first = a._proc.pid
    a._stop.set()                                   # sizeup killed hard: its server keeps running

    same = WhisperService(exe, "turbo.bin", port, 8, state_path=state).start()
    assert wait(lambda: same.state == "ok") and alive(first) and same._proc is None   # reused as-is
    same._stop.set()

    b = WhisperService(exe, "large-v3.bin", port, 8, beam_size=5, state_path=state).start()
    assert wait(lambda: b._proc is not None and b.state == "ok")
    assert wait(lambda: not alive(first) or os.waitpid(first, os.WNOHANG)[0] == first)   # old one stopped
    assert b.argv[-2:] == ["-bs", "5"] and "large-v3.bin" in b.argv
    b.stop()


def test_server_sizeup_did_not_start_is_never_killed(tmp_path):
    import subprocess
    exe, port, state = fake_server(tmp_path), free_port(), tmp_path / "whisper-server.json"
    other = subprocess.Popen([exe, "-m", "x.bin", "--port", str(port)])   # someone else's, no state file
    try:
        assert wait(lambda: WhisperService(exe, "m", port, 1).up())
        svc = WhisperService(exe, "large-v3.bin", port, 8, state_path=state).start()
        assert wait(lambda: svc.state == "ok")
        time.sleep(0.5)
        assert other.poll() is None and svc._proc is None
        svc._stop.set()
    finally:
        other.kill()
