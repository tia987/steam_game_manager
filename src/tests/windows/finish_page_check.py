"""CI, Windows: run a real Inno Setup installer to its Finished page and let the
store's code untick the boxes and press Finish. Neither box may run.

    python tests/windows/finish_page_check.py <installer.exe> <install folder>
"""

import sys
import time
from pathlib import Path

# Run as a script from tests/windows: the package lives two folders up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nsgm import embed, winproc  # noqa: E402

installer, folder = sys.argv[1], Path(sys.argv[2])
launched = winproc.launch(installer, f'/DIR="{folder}" /SP-', cwd=str(Path(installer).parent))
tree = winproc.ProcessTree(launched)
deadline = time.time() + 120
result = None
while tree.alive() and time.time() < deadline:
    if embed.finished_page(tree.pids()):
        time.sleep(1)
        result = embed.finish_unticked(tree.pids())
        print("finish page:", result)
        break
    time.sleep(0.5)
else:
    sys.exit("never reached the Finished page")
tree.wait(timeout=30)
time.sleep(3)                       # a ticked box would have run by now
opened = (folder / "website-opened.txt").exists()
launched_game = (folder / "game-launched.txt").exists()
print("website opened:", opened, "| game launched:", launched_game)
assert (folder / "finish-page.iss").exists(), "the installer did not install"
assert result and result["found"] and result["pressed"], result
assert sorted(result["unticked"]) == ["Launch the game", "Visit the website"], result
assert not opened and not launched_game, "a ticked box still ran"
print("the store unticks an installer's last page: fine")
