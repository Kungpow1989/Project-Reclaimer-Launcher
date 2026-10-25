# Project Reclaimer Launcher

A fan-made launcher for [Project Reclaimer](https://github.com/ProjectReclaimer/project-reclaimer-releases)
for Windows, macOS and Linux.

## Download

Grab the latest build from the
[Releases page](https://github.com/Kungpow1989/Project-Reclaimer-Launcher/releases/latest).
Each release lists SHA-256 checksums in `SHA256SUMS.txt` so you can check
your download:

```
Get-FileHash Reclaimer-Launcher-Windows.exe -Algorithm SHA256
```

## What it does

Open it and it checks GitHub for the latest release. If you're behind, the big
button says **UPDATE** (or **INSTALL** on a fresh setup). Click it, watch the
progress bar, and once the download is verified the button turns into **PLAY**.
The panel underneath lists what's new in the latest release, straight from
its patch notes.

Under the hood it:

1. Asks GitHub for the latest release.
2. Picks out the game client exe (skips the RCON client and customization editor).
3. Compares the SHA256 of your local copy with the release's `SHA256SUMS.txt`.
4. Downloads the new client, verifies the checksum, keeps the old one as
   `.old`, and swaps it in.

Because it checks by hash rather than a saved version number, it also notices
when the game updated itself from its own in-menu updater. If GitHub can't be
reached, the button still says PLAY for the copy you already have.

## macOS and Linux

Project Reclaimer's client is a Windows program and Halo: The Master Chief
Collection is a Windows game, so on macOS and Linux the launcher starts the
client through a compatibility layer. That's set by `runner` in
`launcher.ini`:

| `runner` | What happens |
| --- | --- |
| `auto` (default) | **Linux:** `protontricks-launch --appid 976730`, which runs the client inside the same Proton prefix Steam uses for MCC (works with the Flatpak protontricks on Steam Deck too). Falls back to `wine`. **macOS:** `wine` if it's on your PATH. |
| any command | Your own command, with `{exe}` where the client's path goes. |

Examples:

```
runner = wine {exe}
runner = protontricks-launch --appid 976730 {exe}
runner = /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine --bottle MCC {exe}
```

Whether the client itself runs well under Proton or Wine is up to Project
Reclaimer and your setup. The launcher's own update and install side is the
same on every OS.

## Where things are stored

| OS | Folder |
| --- | --- |
| Windows | Next to `Reclaimer Launcher.exe` (or `%LOCALAPPDATA%\Project Reclaimer Launcher` if that folder isn't writable) |
| macOS | `~/Library/Application Support/Project Reclaimer Launcher` |
| Linux | `~/.local/share/project-reclaimer-launcher` |

**Portable mode:** put a `launcher.ini` next to the launcher and it uses that
folder instead, on any OS. **GAME FOLDER** in the launcher's footer opens
whichever folder is in use.

| File | What it is |
| --- | --- |
| `project-reclaimer-client.exe` | The game client |
| `launcher.ini` | Settings (see below) |
| `launcher_state.json` | Which release is installed |
| `launcher.log` | Log of the last run, handy if something goes wrong |
| `project-reclaimer-client.exe.old` | The previous client, kept after an update |

## Building

The update logic is standard-library Python; the window also uses Pillow.
Ready-made builds come from GitHub Actions, or build locally:

| OS | Command | Result |
| --- | --- | --- |
| Windows | double-click `build.bat` | `dist\Reclaimer Launcher.exe` |
| macOS | `./build.sh` | `release/Reclaimer-Launcher-macOS.zip` (an `.app`) |
| Linux | `./build.sh` | `release/reclaimer-launcher-linux-x86_64.tar.gz` |

You need Python 3.9 or newer with Tk: the python.org installer on Windows and
macOS (or `brew install python python-tk`), and on Linux something like
`sudo apt install python3 python3-venv python3-tk`.

**GitHub Actions:** push this folder to a repo. `.github/workflows/build.yml`
builds all three on GitHub's Windows, macOS (Apple Silicon) and Linux
runners. Run it from the Actions tab to get the builds as artifacts, or push a
tag like `v1.1.0` to attach them to a GitHub release. On an Intel Mac, run
`./build.sh` locally.

**First launch warnings:** the builds aren't code-signed.
- Windows SmartScreen: "More info", then "Run anyway".
- macOS Gatekeeper: right-click the app and choose Open, or run
  `xattr -dr com.apple.quarantine "Reclaimer Launcher.app"`.
- Linux: `chmod +x reclaimer-launcher` if your archive tool dropped the flag.

## Running the tests

The update logic has end-to-end tests that run against a local fake of the
GitHub releases API (Linux or macOS):

```
python tests/test_update_logic.py
```

## Command line options

| Option | Effect |
| --- | --- |
| `--no-update` | Skip the update check |
| `--check-only` | Headless runs only: update if needed but don't start the game |
| `--headless` | No window: check, update, launch, logging to the console |
| anything else | Passed straight through to the game |

## launcher.ini

| Setting | Default | Notes |
| --- | --- | --- |
| `game_exe` | `project-reclaimer-client.exe` | Local name for the client |
| `launch_args` | (empty) | Extra arguments for the game every launch |
| `check_for_updates` | `true` | Set `false` to make it a plain launcher |
| `auto_update` | `false` | Start downloading as soon as an update is found |
| `include_prereleases` | `false` | `true` to also pick up pre-releases |
| `require_checksum` | `true` | Refuse downloads that can't be verified |
| `keep_backup` | `true` | Keep the previous client as `.old` |
| `close_after_launch` | `true` | Close the window once the game starts |
| `borderless` | `auto` | Custom borderless window; `auto` = on for Windows, off for macOS/Linux |
| `runner` | `auto` | How to start the client on macOS/Linux (see above) |

GitHub allows 60 unauthenticated API calls per hour per IP. If that's ever
hit, the launcher falls back to the release page redirect and
`SHA256SUMS.txt`, so it keeps working. A `GITHUB_TOKEN` environment variable
raises the limit if you ever need it.

## Artwork

- The Project Reclaimer wordmark and ring emblem are cut out of the game's own
  loading screen (`tools/source-screenshot.png`) by `tools/make_assets.py`.
- The space backdrop is generated from scratch by the same script.
- Fonts: [Barlow](https://github.com/jpt/barlow) by Jeremy Tribby, SIL Open
  Font License (`assets/fonts/OFL.txt`).

To change the art, edit `tools/make_assets.py` and run it (needs numpy and
Pillow).

## Notes

- If the game is running, the launcher can't replace its file. Close the game
  and click UPDATE again.
- This is a fan-made tool. It isn't affiliated with Project Reclaimer,
  Microsoft, Xbox Game Studios, Halo Studios, 343 Industries or Bungie.
