"""
Project Reclaimer Launcher

Checks GitHub for the latest Project Reclaimer release, downloads the game
client if the local copy is missing or out of date, verifies it against the
release's SHA256SUMS.txt, then starts the game.

Runs on Windows, macOS and Linux. The game client itself is a Windows
program, so on macOS and Linux it is started through Proton or Wine (see the
"runner" setting).

The update logic is stdlib only; the window (gui.py) also uses Pillow.

Usage:
    "Reclaimer Launcher.exe"                 check, update if needed, launch
    "Reclaimer Launcher.exe" --no-update     skip the check and just launch
    "Reclaimer Launcher.exe" --check-only    check/update but don't launch
    "Reclaimer Launcher.exe" --headless      no window, log to console/file
    Anything else on the command line is passed through to the game.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Project Reclaimer Launcher"
LAUNCHER_VERSION = "1.1.0"
USER_AGENT = f"ReclaimerLauncher/{LAUNCHER_VERSION}"

DEFAULT_CONFIG = {
    "repo": "ProjectReclaimer/project-reclaimer-releases",
    # Local file name the client is saved as. Kept stable so shortcuts work
    # and so the game's own in-menu updater (which keeps its file name) and
    # this launcher agree on which file is the game.
    "game_exe": "project-reclaimer-client.exe",
    # Extra arguments passed to the game every launch.
    "launch_args": "",
    "check_for_updates": "true",
    "include_prereleases": "false",
    # Refuse to install a download that can't be checked against a SHA256.
    "require_checksum": "true",
    # Keep the previous client as <game_exe>.old after an update.
    "keep_backup": "true",
    # Close the launcher window once the game has started.
    "close_after_launch": "true",
    # Start downloading as soon as an update is found, without waiting for
    # the UPDATE button.
    "auto_update": "false",
    # Draw the launcher without the normal title bar. "auto" means borderless
    # on Windows and a normal window on macOS and Linux.
    "borderless": "auto",
    # macOS/Linux only: how to start the Windows game client.
    #   auto  -> Linux: protontricks-launch into MCC's Proton prefix (Steam
    #            app 976730), else wine. macOS: wine if it's on the PATH.
    #   or a command, with {exe} where the client's path goes, e.g.
    #   runner = wine {exe}
    #   runner = /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine --bottle MCC {exe}
    "runner": "auto",
    # Only change these for testing.
    "api_base": "https://api.github.com",
    "web_base": "https://github.com",
}

# Asset names seen so far:
#   v0.9.x : project-reclaimer-client-v0.9.21.exe
#   v0.8.6+: project-reclaimer-v0.8.13.exe
#   older  : project-reclaimer.exe
# Releases also ship reclaimer-rcon-*.exe and
# reclaimer-customization-editor-*.exe, which we must skip.
CLIENT_PATTERN = re.compile(r"^project-reclaimer(?:-client)?(?:-v?[\w.\-]+)?\.exe$", re.I)
EXCLUDE_WORDS = ("rcon", "editor", "customization", "dedicated", "server")
SUMS_NAMES = ("SHA256SUMS.txt", "SHA256SUMS", "sha256sums.txt", "checksums.txt")


# --------------------------------------------------------------------------
# Paths, config, logging
# --------------------------------------------------------------------------

def resource_dir() -> Path:
    """Folder holding bundled assets (PyInstaller unpacks them to _MEIPASS)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def app_dir() -> Path:
    """Folder the launcher lives in (the exe's folder when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


MCC_STEAM_APPID = "976730"
DATA_FOLDER_NAME = "Project Reclaimer Launcher"


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """Where the game, settings and logs live.

    Portable mode: if launcher.ini sits next to the launcher, use that folder.
    Otherwise Windows uses the launcher's own folder (falling back to
    %LOCALAPPDATA% if it isn't writable), macOS uses Application Support
    (never inside the .app bundle) and Linux uses $XDG_DATA_HOME.
    """
    here = app_dir()
    if (here / "launcher.ini").exists():
        return here
    if os.name == "nt":
        if _writable(here):
            return here
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        d = root / DATA_FOLDER_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / DATA_FOLDER_NAME
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        d = root / "project-reclaimer-launcher"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config(base: Path) -> dict:
    path = base / "launcher.ini"
    cp = configparser.ConfigParser(interpolation=None)
    cp["launcher"] = DEFAULT_CONFIG
    if path.exists():
        cp.read(path, encoding="utf-8")
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("; Project Reclaimer Launcher settings\n")
                cp.write(f)
        except OSError:
            pass
    return dict(cp["launcher"])


def as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def setup_logging(base: Path, headless: bool) -> logging.Logger:
    log = logging.getLogger("launcher")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        fh = logging.FileHandler(base / "launcher.log", mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if headless and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http_get(url: str, accept: str = "*/*", timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def resolve_redirect(url: str, timeout: float = 20) -> str:
    """Return the final URL after redirects (used for /releases/latest)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl()


def download(url: str, dest: Path, progress=None, timeout: float = 30) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = r.read1(256 * 1024) if hasattr(r, "read1") else r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and (now - last > 0.1 or done == total):
                progress(done, total)
                last = now
        if progress:
            progress(done, total or done)
        if total and done != total:
            raise IOError(f"Download incomplete: got {done} of {total} bytes")


# --------------------------------------------------------------------------
# Release logic (pure functions, easy to test)
# --------------------------------------------------------------------------

@dataclass
class Release:
    tag: str
    assets: dict = field(default_factory=dict)   # name -> download url
    digests: dict = field(default_factory=dict)  # name -> sha256 hex (from API)
    html_url: str = ""
    body: str = ""


@dataclass
class UpdateInfo:
    latest_tag: str
    asset: str
    expected: str | None          # sha256 the client should have
    needs_update: bool
    installed: bool               # is there a client on disk at all
    installed_tag: str | None     # what we think is installed
    html_url: str
    notes: list                   # feature headlines from the release notes
    release: Release


def parse_notes(body: str, limit: int = 8) -> list:
    """Pull the feature headlines ("### ...") out of a release's notes."""
    out = []
    section = ""
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].strip().lower()
        elif s.startswith("### ") and (not section or "feature" in section or "new" in section):
            title = re.sub(r"[*_`]", "", s[4:]).strip()
            if title:
                out.append(title)
        if len(out) >= limit:
            break
    if not out:  # fall back to bolded bullet points, e.g. under "Changes"
        for line in (body or "").splitlines():
            m = re.match(r"^\s*[-*]\s+\*\*(.+?)\*\*", line)
            if m:
                out.append(m.group(1).rstrip("."))
            if len(out) >= limit:
                break
    return out


def pick_client_asset(names) -> str | None:
    candidates = []
    for n in names:
        low = n.lower()
        if any(w in low for w in EXCLUDE_WORDS):
            continue
        if CLIENT_PATTERN.match(n):
            candidates.append(n)
    if not candidates:
        return None
    # Prefer an explicit "client" build if both kinds exist.
    candidates.sort(key=lambda n: (0 if "client" in n.lower() else 1, n))
    return candidates[0]


def parse_sums(text: str) -> dict:
    """Parse sha256sum-style lines: '<hex>  name', '<hex> *name', or 'name: <hex>'."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if m:
            out[Path(m.group(2).strip()).name] = m.group(1).lower()
            continue
        m = re.match(r"^(.+?)[:\s]+([0-9a-fA-F]{64})$", line)
        if m:
            out[Path(m.group(1).strip()).name] = m.group(2).lower()
    return out


def version_key(tag: str):
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def release_from_api(data: dict) -> Release:
    rel = Release(tag=data.get("tag_name", ""), html_url=data.get("html_url", ""),
                  body=data.get("body") or "")
    for a in data.get("assets", []):
        rel.assets[a["name"]] = a["browser_download_url"]
        d = a.get("digest") or ""
        if d.lower().startswith("sha256:"):
            rel.digests[a["name"]] = d.split(":", 1)[1].lower()
    return rel


def fetch_latest(cfg: dict, log: logging.Logger) -> Release:
    repo = cfg["repo"]
    api = cfg["api_base"].rstrip("/")
    web = cfg["web_base"].rstrip("/")
    try:
        if as_bool(cfg["include_prereleases"]):
            data = json.loads(http_get(f"{api}/repos/{repo}/releases?per_page=10",
                                       "application/vnd.github+json"))
            data = next(r for r in data if not r.get("draft"))
        else:
            data = json.loads(http_get(f"{api}/repos/{repo}/releases/latest",
                                       "application/vnd.github+json"))
        rel = release_from_api(data)
        if rel.tag:
            return rel
        raise ValueError("API response had no tag_name")
    except Exception as e:  # rate limit, API down, etc.
        log.warning("GitHub API lookup failed (%s); falling back to release page", e)

    # Fallback: /releases/latest redirects to /releases/tag/<tag>. The asset
    # list comes from SHA256SUMS.txt, which names every file in the release.
    final = resolve_redirect(f"{web}/{repo}/releases/latest")
    m = re.search(r"/releases/tag/([^/?#]+)", final)
    if not m:
        raise RuntimeError(f"Could not work out the latest release from {final}")
    tag = urllib.request.unquote(m.group(1))
    rel = Release(tag=tag, html_url=final)
    base = f"{web}/{repo}/releases/download/{tag}"
    for sums_name in SUMS_NAMES:
        try:
            sums = parse_sums(http_get(f"{base}/{sums_name}").decode("utf-8", "replace"))
        except urllib.error.HTTPError:
            continue
        rel.assets[sums_name] = f"{base}/{sums_name}"
        for name in sums:
            rel.assets[name] = f"{base}/{name}"
        break
    return rel


def fetch_sums(rel: Release, log: logging.Logger) -> dict:
    for name in SUMS_NAMES:
        if name in rel.assets:
            try:
                return parse_sums(http_get(rel.assets[name]).decode("utf-8", "replace"))
            except Exception as e:
                log.warning("Could not read %s: %s", name, e)
    return {}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def read_state(base: Path) -> dict:
    try:
        return json.loads((base / "launcher_state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(base: Path, state: dict) -> None:
    try:
        (base / "launcher_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Main worker
# --------------------------------------------------------------------------

class UpdateError(Exception):
    pass


def check_for_update(base: Path, cfg: dict, log: logging.Logger, status=lambda m: None) -> UpdateInfo:
    """Find the latest release and work out whether the local client needs it."""
    game = base / cfg["game_exe"]
    state = read_state(base)

    status("Checking for updates...")
    rel = fetch_latest(cfg, log)
    log.info("Latest release: %s (%d assets)", rel.tag, len(rel.assets))

    asset = pick_client_asset(rel.assets.keys())
    if not asset:
        raise UpdateError(f"Release {rel.tag} has no game client exe I recognise.\n"
                          f"Assets: {', '.join(rel.assets) or 'none'}")
    log.info("Client asset: %s", asset)

    sums = fetch_sums(rel, log)
    expected = sums.get(asset) or rel.digests.get(asset)
    if rel.digests.get(asset) and sums.get(asset) and rel.digests[asset] != sums[asset]:
        raise UpdateError("GitHub's digest and SHA256SUMS.txt disagree for "
                          f"{asset}. Not installing it.")

    info = UpdateInfo(latest_tag=rel.tag, asset=asset, expected=expected, needs_update=True,
                      installed=game.exists(), installed_tag=state.get("tag"),
                      html_url=rel.html_url or f"{cfg['web_base'].rstrip('/')}/{cfg['repo']}/releases/latest",
                      notes=parse_notes(rel.body), release=rel)

    # Is the local copy already this exact build? Comparing hashes means we
    # also notice when the game updated itself from its own menu.
    if game.exists():
        if expected:
            status("Checking installed version...")
            local = sha256_file(game)
            if local == expected:
                log.info("Local client matches %s", rel.tag)
                if state.get("tag") != rel.tag:
                    write_state(base, {**state, "tag": rel.tag, "asset": asset, "sha256": local})
                info.needs_update = False
                info.installed_tag = rel.tag
                return info
            log.info("Local client hash %s differs from %s", local[:12], expected[:12])
            if state.get("sha256") and state["sha256"] != local:
                info.installed_tag = None  # changed since we installed it; version unknown
        elif state.get("tag") and version_key(state["tag"]) >= version_key(rel.tag):
            info.needs_update = False
            return info

    if not expected and as_bool(cfg["require_checksum"]):
        raise UpdateError(f"Release {rel.tag} has no checksum for {asset}, so it "
                          "can't be verified. Set require_checksum = false in "
                          "launcher.ini to install it anyway.")
    return info


def update_if_needed(base: Path, cfg: dict, log: logging.Logger, status, progress) -> str:
    """Make sure the newest client is installed. Returns a short result message."""
    info = check_for_update(base, cfg, log, status)
    if not info.needs_update:
        return f"Up to date ({info.installed_tag or info.latest_tag})"
    return install_update(base, cfg, info, log, status, progress)


def install_update(base: Path, cfg: dict, info: UpdateInfo, log: logging.Logger, status, progress) -> str:
    """Download, verify and swap in the client described by `info`."""
    game = base / cfg["game_exe"]
    rel, asset, expected = info.release, info.asset, info.expected

    status(f"Downloading {rel.tag}...")
    part = game.with_name(game.name + ".part")
    try:
        download(rel.assets[asset], part, progress)
        if expected:
            status("Verifying download...")
            got = sha256_file(part)
            if got != expected:
                raise UpdateError(f"Checksum mismatch for {asset}.\nExpected {expected}\nGot      {got}\n"
                                  "The download was discarded.")
            log.info("SHA256 verified: %s", got)
        else:
            log.warning("Installed %s without checksum verification", asset)

        status("Installing...")
        if game.exists() and as_bool(cfg["keep_backup"]):
            backup = game.with_name(game.name + ".old")
            try:
                if backup.exists():
                    backup.unlink()
                os.replace(game, backup)
            except PermissionError:
                raise UpdateError("Couldn't replace the game file. Is Project Reclaimer "
                                  "still running? Close it and try again.")
        try:
            os.replace(part, game)
            if os.name != "nt":  # lets the launcher be tested on Linux/Wine setups
                os.chmod(game, 0o755)
        except PermissionError:
            raise UpdateError("Couldn't replace the game file. Is Project Reclaimer "
                              "still running? Close it and try again.")
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

    write_state(base, {"tag": rel.tag, "asset": asset, "sha256": expected or "",
                       "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    log.info("Installed %s", rel.tag)
    return f"Updated to {rel.tag}"


def runner_command(cfg: dict, game: Path) -> list:
    """The command that starts the Windows client on this OS."""
    if os.name == "nt":
        return [str(game)]
    r = (cfg.get("runner") or "auto").strip()
    if r and r.lower() != "auto":
        parts = shlex.split(r)
        if any("{exe}" in x for x in parts):
            return [x.replace("{exe}", str(game)) for x in parts]
        return parts + [str(game)]
    if sys.platform.startswith("linux"):
        # Run inside MCC's own Proton prefix so the client finds Halo 3.
        if shutil.which("protontricks-launch"):
            return ["protontricks-launch", "--appid", MCC_STEAM_APPID, str(game)]
        if shutil.which("flatpak"):
            try:
                ok = subprocess.run(["flatpak", "info", "com.github.Matoking.protontricks"],
                                    capture_output=True, timeout=10).returncode == 0
            except Exception:
                ok = False
            if ok:  # Steam Deck and other Flatpak setups
                return ["flatpak", "run", "--command=protontricks-launch",
                        "com.github.Matoking.protontricks", "--appid", MCC_STEAM_APPID, str(game)]
    for w in ("wine", "wine64"):
        if shutil.which(w):
            return [w, str(game)]
    if sys.platform == "darwin":
        raise UpdateError("macOS needs Wine, CrossOver or Whisky to run the Windows game "
                          "client. Install one, then set runner in launcher.ini.")
    raise UpdateError("Linux needs protontricks or Wine to run the Windows game client. "
                      "Install one, or set runner in launcher.ini.")


def launch_game(base: Path, cfg: dict, extra_args: list, log: logging.Logger) -> None:
    game = base / cfg["game_exe"]
    if not game.exists():
        raise UpdateError("The game isn't installed yet and the update check failed, "
                          "so there's nothing to launch. Check your internet connection.")
    args = runner_command(cfg, game) + cfg["launch_args"].split() + extra_args
    log.info("Launching: %s", args)
    kwargs = {"cwd": str(base)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def run(base: Path, cfg: dict, opts: dict, log: logging.Logger, status, progress) -> str:
    result = "Skipped update check"
    if opts["update"] and as_bool(cfg["check_for_updates"]):
        try:
            result = update_if_needed(base, cfg, log, status, progress)
        except UpdateError:
            raise
        except Exception as e:
            log.exception("Update check failed")
            if not (base / cfg["game_exe"]).exists():
                raise UpdateError(f"Couldn't download the game: {e}")
            result = f"Update check failed ({e}); launching installed version"
    status(result)
    if opts["launch"]:
        launch_game(base, cfg, opts["extra"], log)
    return result


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def run_headless(base, cfg, opts, log) -> int:
    last = {"pct": -1}

    def status(msg):
        log.info(msg)

    def progress(done, total):
        if total:
            pct = int(done * 100 / total)
            if pct // 10 != last["pct"] // 10:
                last["pct"] = pct
                log.info("Downloaded %d%%", pct)

    try:
        run(base, cfg, opts, log, status, progress)
        return 0
    except Exception as e:
        log.error("%s", e)
        return 1


def parse_args(argv):
    opts = {"update": True, "launch": True, "headless": False, "extra": []}
    for a in argv:
        if a == "--no-update":
            opts["update"] = False
        elif a == "--check-only":
            opts["launch"] = False
        elif a == "--headless":
            opts["headless"] = True
        elif a in ("--version", "-V"):
            print(f"{APP_NAME} {LAUNCHER_VERSION}")
            sys.exit(0)
        else:
            opts["extra"].append(a)
    return opts


def main() -> int:
    opts = parse_args(sys.argv[1:])
    base = data_dir()
    cfg = load_config(base)
    log = setup_logging(base, opts["headless"])
    log.info("%s %s in %s", APP_NAME, LAUNCHER_VERSION, base)
    if not opts["headless"]:
        try:
            from gui import run_gui
            return run_gui(base, cfg, opts, log)
        except ImportError as e:
            log.warning("GUI not available (%s), running headless", e)
        except Exception as e:  # e.g. no display
            if "display" not in str(e).lower():
                raise
            log.warning("No display (%s), running headless", e)
    return run_headless(base, cfg, opts, log)


if __name__ == "__main__":
    sys.exit(main())
