"""CI, Windows: a store install's tidy-up. A console the "installer" opens must
be hidden, and a "browser" window whose title turns into a bit.ly page after
the install started must get Ctrl+W (here: a small window that closes itself
on Ctrl+W and leaves a file saying so).

    python tests/windows/tidy_check.py <work folder>
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nsgm import embed, winproc  # noqa: E402

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
marker = work / "tab-closed.txt"

page = f"""
import tkinter as tk
root = tk.Tk()
root.title("New Tab - Test Browser")
root.geometry("500x300+100+100")
def close(_event=None):
    open(r"{marker}", "w").write("closed")
    root.destroy()
root.bind_all("<Control-w>", close)
root.after(2500, lambda: root.title("Bitly | bit.ly/fitgirl-repacks-site - Test Browser"))
root.after(30000, root.destroy)
root.mainloop()
"""
browser = subprocess.Popen([sys.executable, "-c", page])
time.sleep(1.5)                      # the browser was open before the install

console = subprocess.Popen(["cmd.exe", "/c", "title Applying redirection rules & ping -n 15 127.0.0.1 >nul"],
                           creationflags=subprocess.CREATE_NEW_CONSOLE)
tree = winproc.ProcessTree(winproc.Launched(console.pid, popen=console))
browser_name = Path(sys.executable).name.lower()
tidy = embed.Tidy(tree, lambda name: (name or "").lower() == browser_name).start()

deadline = time.time() + 12
hidden_seen = False
while time.time() < deadline:
    consoles = [h for h in embed._top_level_windows()
                if embed._pid(h) in tree.pids() and embed._class(h) in embed.CONSOLE_CLASSES]
    hidden_seen = hidden_seen or bool(tidy.hidden)
    if marker.exists() and hidden_seen:
        break
    time.sleep(0.25)
print("notes:", tidy.notes)
print("console windows of the tree:", [(embed._class(h), embed._IsWindowVisible(h)) for h in consoles])
tidy.stop()
console.kill()
browser.kill()
assert hidden_seen, "the installer's console was not hidden"
assert all(not embed._IsWindowVisible(h) for h in consoles), "a console is still visible"
assert marker.exists(), "the bit.ly page was not closed"
print("the tidy-up hides installer consoles and closes the page they open: fine")
