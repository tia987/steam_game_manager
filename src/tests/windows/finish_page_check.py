"""CI, Windows: run a real Inno Setup installer to its Finished page and let the
store's own code deal with it. Neither ticked box may run, nor the code the
installer runs as it exits (where repacks open their website).

    python tests/windows/finish_page_check.py <installer.exe> <install folder>
"""

import sys
import time
from pathlib import Path

# Run as a script from tests/windows: the package lives two folders up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nsgm import autoinstall, embed, winproc  # noqa: E402

installer, folder = str(Path(sys.argv[1]).resolve()), Path(sys.argv[2]).resolve()
launched = winproc.launch(installer, f'/DIR="{folder}" /SP-', cwd=str(Path(installer).parent))
tree = winproc.ProcessTree(launched)
deadline = time.time() + 120


def press_through(pids):
    """Stand in for the person on the pages before the last: Inno never
    skips its Ready page when it is the only one."""
    for hwnd in embed._installer_windows(pids):
        for kid in embed._child_windows(hwnd):
            if (embed._IsWindowVisible(kid) and embed._IsWindowEnabled(kid)
                    and embed._class(kid) in embed.BUTTON_CLASSES
                    and embed._caption(kid) in ("install", "next", "ok")):
                embed._send(kid, embed.BM_CLICK)
                return


task = autoinstall.Task(id="ci", path=installer, name="Finish page test")
closed = False
seen = 0.0
while tree.alive() and time.time() < deadline:
    if not embed.finished_page(tree.pids()):
        seen = 0.0
        press_through(tree.pids())
    elif seen and time.time() - seen > 1.0:
        closed, _tried = autoinstall.QUEUE._close_at_finish(task, tree, False)
        print("finish page:", closed, task.log, task.warning)
        break
    else:
        seen = seen or time.time()
    time.sleep(0.5)
else:
    sys.exit("never reached the Finished page")
tree.wait(timeout=30)
time.sleep(3)                       # anything still to run would have by now
present = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
print("in the folder:", present)
assert "finish-page.iss" in present, "the installer did not install"
assert closed, "the store did not close the installer on its last page"
assert "closed" in task.log[-1], task.log
assert "website-opened.txt" not in present, "a ticked box still ran"
assert "game-launched.txt" not in present, "a ticked box still ran"
assert "exit-code-ran.txt" not in present, "the installer's exit code still ran"
print("the store closes an installer on its last page, running nothing more: fine")

# Control: the same installer, finished the old way (boxes unticked, then
# Finish). The boxes stay quiet but the exit code still runs: that is how a
# repack opened its website after "unticking everything".
control = folder.parent / (folder.name + "-control")
tree = winproc.ProcessTree(winproc.launch(installer, f'/DIR="{control}" /SP-',
                                          cwd=str(Path(installer).parent)))
deadline = time.time() + 120
while tree.alive() and time.time() < deadline:
    if embed.finished_page(tree.pids()):
        time.sleep(1)
        print("control:", embed.finish_unticked(tree.pids()))
        break
    press_through(tree.pids())
    time.sleep(0.5)
tree.wait(timeout=30)
time.sleep(3)
present = sorted(p.name for p in control.iterdir()) if control.exists() else []
print("control folder:", present)
assert "website-opened.txt" not in present and "game-launched.txt" not in present, present
assert "exit-code-ran.txt" in present, "control: Finish should have run the exit code"
print("control: pressing Finish runs the exit code even with every box unticked, as expected")
