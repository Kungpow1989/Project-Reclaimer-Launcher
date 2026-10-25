"""
End-to-end tests for the update logic, run against a local fake of the
GitHub releases API. Linux/macOS only (the fake "game" is a shell script).

    python tests/test_update_logic.py
"""
import hashlib, http.server, json, os, shutil, subprocess, sys, threading, stat
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "launcher.py"
import tempfile
WORK = Path(tempfile.mkdtemp(prefix="reclaimer-test-")) / "t"
REPO = "ProjectReclaimer/project-reclaimer-releases"

STATE = {"tag": "v0.9.20", "api_ok": True, "bad_sum": False, "digest": True}

def client_bytes(tag):
    # a tiny "game" shell script so launching can be observed on linux
    return f"#!/bin/sh\necho {tag} \"$@\" > \"$(dirname \"$0\")/launched.txt\"\n".encode() + b"x" * 300000

def assets(tag):
    exe = client_bytes(tag)
    files = {
        f"project-reclaimer-client-{tag}.exe": exe,
        f"reclaimer-rcon-{tag}.exe": b"rcon",
        f"reclaimer-customization-editor-{tag}.exe": b"editor",
        "RCON.md": b"# rcon",
    }
    sums = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in files.items())
    if STATE["bad_sum"]:
        sums = sums.replace(hashlib.sha256(exe).hexdigest(), "0" * 64)
    files["SHA256SUMS.txt"] = sums.encode()
    return files

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        tag = STATE["tag"]; files = assets(tag); host = f"http://127.0.0.1:{self.server.server_port}"
        p = self.path
        if p == f"/api/repos/{REPO}/releases/latest":
            if not STATE["api_ok"]:
                return self.send(403, b'{"message":"API rate limit exceeded"}')
            data = {"tag_name": tag, "html_url": "x", "assets": [
                {"name": n, "browser_download_url": f"{host}/web/{REPO}/releases/download/{tag}/{n}",
                 **({"digest": "sha256:" + hashlib.sha256(b).hexdigest()} if STATE["digest"] and n != "SHA256SUMS.txt" else {})}
                for n, b in files.items()]}
            return self.send(200, json.dumps(data).encode())
        if p == f"/web/{REPO}/releases/latest":
            return self.send(302, headers={"Location": f"{host}/web/{REPO}/releases/tag/{tag}"})
        if p == f"/web/{REPO}/releases/tag/{tag}":
            return self.send(200, b"<html>release</html>")
        pre = f"/web/{REPO}/releases/download/{tag}/"
        if p.startswith(pre) and p[len(pre):] in files:
            return self.send(200, files[p[len(pre):]])
        self.send(404, b"nope")

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_port

shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir(parents=True)
shutil.copy(SRC, WORK / "launcher.py"); shutil.copy(SRC.parent / "gui.py", WORK / "gui.py"); shutil.copytree(SRC.parent / "assets", WORK / "assets")
(WORK / "launcher.ini").write_text(f"""[launcher]
api_base = http://127.0.0.1:{port}/api
web_base = http://127.0.0.1:{port}/web
runner = {{exe}}
""")
GAME = WORK / "project-reclaimer-client.exe"

def run(*args):
    (WORK / "launched.txt").unlink(missing_ok=True)
    r = subprocess.run([sys.executable, "-I", str(WORK / "launcher.py"), "--headless", *args],
                       capture_output=True, text=True, cwd="/", env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()})
    import time; time.sleep(0.3)
    if GAME.exists(): GAME.chmod(GAME.stat().st_mode | stat.S_IEXEC)
    launched = (WORK / "launched.txt").read_text().strip() if (WORK / "launched.txt").exists() else None
    msgs = [l.split(" ", 3)[-1] for l in r.stdout.splitlines()]
    return r.returncode, launched, msgs

def check(name, cond, info):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: print("   ", info)

# Popen of a non-executable fresh download fails on linux, so make downloaded files executable via umask trick:
# instead, run launcher twice where needed. Simpler: patch os.replace target exec bit after run (done in run()).

# 1 fresh install
rc, l, m = run("--check-only")
check("fresh install downloads v0.9.20", rc == 0 and GAME.exists() and any("Installed v0.9.20" in x for x in m), m)
check("picked the client, not rcon/editor", any("project-reclaimer-client-v0.9.20.exe" in x for x in m), m)
check("state written", json.loads((WORK/"launcher_state.json").read_text())["tag"] == "v0.9.20", "")

# 2 up to date, launches with passthrough args
rc, l, m = run("--foo")
check("up to date + launch with args", rc == 0 and any("Up to date (v0.9.20)" in x for x in m) and l == "v0.9.20 --foo", (m, l))

# 3 new release
STATE["tag"] = "v0.9.21"
rc, l, m = run()
check("updates to v0.9.21 and launches it", rc == 0 and l == "v0.9.21" and (WORK/"project-reclaimer-client.exe.old").exists(), (m, l))

# 4 game self-updated (hash already matches latest, state stale)
STATE["tag"] = "v0.9.22"
GAME.write_bytes(client_bytes("v0.9.22")); GAME.chmod(0o755)
rc, l, m = run("--check-only")
check("detects in-game self update by hash, no download", rc == 0 and any("Up to date (v0.9.22)" in x for x in m) and not any("Downloading" in x for x in m), m)
check("state tag synced", json.loads((WORK/"launcher_state.json").read_text())["tag"] == "v0.9.22", "")

# 5 bad checksum -> refuse, keep old exe, still... error exit
STATE["tag"] = "v0.9.23"; STATE["bad_sum"] = True
before = GAME.read_bytes()
rc, l, m = run()
check("checksum mismatch refused, old exe kept, no .part left", rc == 1 and GAME.read_bytes() == before and not (WORK/"project-reclaimer-client.exe.part").exists() and any("disagree" in x or "mismatch" in x for x in m), m)
STATE["bad_sum"] = False

# 6 API rate-limited -> fallback via release page + SHA256SUMS
STATE["api_ok"] = False
rc, l, m = run()
check("API down: falls back to release page and updates", rc == 0 and l == "v0.9.23" and any("falling back" in x for x in m), m)
STATE["api_ok"] = True

# 7 no digest in API, only SHA256SUMS
STATE["tag"] = "v0.9.24"; STATE["digest"] = False
rc, l, m = run()
check("works with SHA256SUMS only", rc == 0 and l == "v0.9.24", m)

# 8 offline with game installed -> launches installed version
srv.shutdown(); srv.server_close()
rc, l, m = run()
check("offline: launches installed copy", rc == 0 and l == "v0.9.24" and any("Update check failed" in x for x in m), m)

# 9 offline, nothing installed -> clear error
for f in WORK.glob("project-reclaimer-client.exe*"): f.unlink()
rc, l, m = run()
check("offline + not installed: error", rc == 1 and any("Couldn't download the game" in x for x in m), m)

# 10 --no-update skips network
rc, l, m = run("--no-update")
check("--no-update with no game: error message", rc == 1, m)
