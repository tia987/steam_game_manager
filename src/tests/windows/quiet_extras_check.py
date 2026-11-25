"""CI, Windows: install a repack-like installer silently, as the store does, and
check that the store's watcher stops its redistributable and its website
before either does anything. A control run without the watcher shows that
Inno Setup does run both in a silent install.

    python tests/windows/quiet_extras_check.py <installer.exe> <work folder>
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nsgm import autoinstall, installers, winproc  # noqa: E402

installer, work = str(Path(sys.argv[1]).resolve()), Path(sys.argv[2]).resolve()
work.mkdir(parents=True, exist_ok=True)


def install(folder: Path, guarded: bool) -> tuple[list, list]:
    log = str(work / f"{folder.name}.log")
    command = installers.quiet_command({"framework": "inno", "source": installer},
                                       str(folder), log)
    tree = winproc.ProcessTree(winproc.launch(command["file"], command["params"],
                                              cwd=str(Path(installer).parent)))
    task = autoinstall.Task(id=folder.name, path=installer, name="Quiet extras test",
                            skip_redists=True, skip_checkers=True, skip_finish=True)
    guard = autoinstall.SideGuard(autoinstall.QUEUE, task, tree).start() if guarded else None
    deadline = time.time() + 120
    while tree.alive() and time.time() < deadline:
        time.sleep(0.2)
    if guard:
        guard.stop()
    time.sleep(6)                    # anything left running writes its file by now
    ran = autoinstall.run_entries(Path(log).read_text("utf-8", "replace"))
    present = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    print(folder.name, "| folder:", present, "| notes:", task.log, "| run entries:", ran)
    return present, ran


control, ran = install(work / "control", guarded=False)
assert "quiet-extras.iss" in control, "control: the installer did not install"
assert "redist-ran.txt" in control, "control: the redistributable should have run"
assert "website-opened.txt" in control, "control: Inno should run the website entry silently"
assert any("bit.ly" in entry for entry in ran), ran
print("control: a silent install runs the redistributable and the website, as expected")

guarded, _ran = install(work / "guarded", guarded=True)
assert "quiet-extras.iss" in guarded, "the installer did not install"
assert "redist-ran.txt" not in guarded, "the redistributable was not stopped in time"
assert "website-opened.txt" not in guarded, "the website was not stopped in time"
print("the store stops a silent install's redistributable and website: fine")
