"""Add an "Agent Playbook" shortcut that opens the canvas.
  Windows: Desktop and Start menu shortcuts (no console window).
  macOS:   ~/Desktop/Agent Playbook.command
  Linux:   ~/.local/share/applications/agent-playbook.desktop
Run once after installing the skill:  python playbook/install_shortcut.py   (add --remove to undo)"""
import argparse
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
LAUNCH = HERE / "launch.pyw"
ICON = HERE / "playbook.ico"
NAME = "Agent Playbook"


def write_lf(path, text):
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def windows(remove):
    appdata = pathlib.Path(os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming"))
    # ask Windows for the real Desktop folder (it may be redirected to OneDrive)
    desk = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                          capture_output=True, text=True, creationflags=0x08000000).stdout.strip() or str(pathlib.Path.home() / "Desktop")
    targets = [pathlib.Path(desk) / f"{NAME}.lnk", appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{NAME}.lnk"]
    if remove:
        for t in targets:
            t.unlink(missing_ok=True)
        return targets
    pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else pathlib.Path(sys.executable)
    for t in targets:
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PB_LNK);"
              "$s.TargetPath=$env:PB_EXE;$s.Arguments=('\"'+$env:PB_LAUNCH+'\"');$s.WorkingDirectory=$env:PB_DIR;"
              "$s.Description='Open the Agent Playbook canvas';if(Test-Path $env:PB_ICON){$s.IconLocation=$env:PB_ICON};$s.Save()")
        env = dict(os.environ, PB_LNK=str(t), PB_EXE=str(exe), PB_LAUNCH=str(LAUNCH), PB_DIR=str(HERE), PB_ICON=str(ICON))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=True, creationflags=0x08000000)
    return targets


def mac(remove):
    t = pathlib.Path.home() / "Desktop" / f"{NAME}.command"
    if remove:
        t.unlink(missing_ok=True)
        return [t]
    write_lf(t, f'#!/bin/sh\n"{sys.executable}" "{LAUNCH}"\n')
    t.chmod(0o755)
    return [t]


def linux(remove):
    t = pathlib.Path.home() / ".local" / "share" / "applications" / "agent-playbook.desktop"
    if remove:
        t.unlink(missing_ok=True)
        return [t]
    t.parent.mkdir(parents=True, exist_ok=True)
    write_lf(t, f"[Desktop Entry]\nType=Application\nName={NAME}\nComment=Open the Agent Playbook canvas\n"
                f'Exec="{sys.executable}" "{LAUNCH}"\nTerminal=false\nCategories=Office;\n')
    return [t]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install_shortcut.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remove", action="store_true", help="take the shortcuts away instead of adding them")
    args = ap.parse_args(argv)  # --help prints this and exits before anything is created
    fn = windows if os.name == "nt" else mac if sys.platform == "darwin" else linux
    try:
        made = fn(args.remove)
    except (OSError, subprocess.CalledProcessError) as e:
        print("could not %s the shortcut: %s" % ("remove" if args.remove else "create", e), file=sys.stderr)
        sys.exit(1)
    for p in made:
        print(("removed " if args.remove else "created ") + str(p))


if __name__ == "__main__":
    main()
