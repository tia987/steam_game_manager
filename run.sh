#!/usr/bin/env bash
# Non-Steam Game Manager
# Uses shell builtins only until Python is found, so this still prints
# something useful on a minimal or recovery shell.
cd "${0%/*}" 2>/dev/null || cd "$(dirname "$0")" || exit 1

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
        PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$PWD/src" exec "$candidate" -m nsgm "$@"
    fi
done

printf '%s\n' \
  '' \
  '  Python 3.9 or newer is needed but was not found.' \
  '' \
  '    Debian, Ubuntu, Mint     sudo apt install python3' \
  '    Fedora                   sudo dnf install python3' \
  '    Arch, SteamOS            sudo pacman -S python' \
  '    openSUSE                 sudo zypper install python3' \
  '' \
  '  On a Steam Deck the system partition is read-only. Either run' \
  '  "steamos-readonly disable" first, or work inside a distrobox.' \
  ''
exit 1
