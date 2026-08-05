# Building and publishing

The app runs from source with nothing installed. These notes are for
turning it into a single file other people can double-click.

## Running from source

```
python -m nsgm
```

Python 3.9 or newer, no dependencies. That is the whole requirement.

## Building one executable

```
pip install pyinstaller
pyinstaller nsgm.spec --clean --noconfirm
```

The result lands in `dist/`: about 10 MB, self-contained, no Python needed
on the machine that runs it.

**A build only produces a binary for the system it runs on.** PyInstaller
does not cross-compile, so a Windows `.exe` has to be built on Windows.
If you do not have a Windows machine, the next section solves exactly
that problem.

## Letting GitHub build it

`.github/workflows/build.yml` builds for Windows, Linux and macOS on
GitHub's own runners. You never need the other machines.

Setting it up:

1. Create a repository and push this folder to it.
2. That is all. The workflow runs on every push to `main` and on every
   pull request, checking the code imports and that the binary starts.
3. To publish, tag a version and push the tag:

   ```
   git tag v1.0.0
   git push origin v1.0.0
   ```

   The three binaries get built, named per platform, checksummed, and
   attached to a GitHub release automatically. People download one file
   and run it.

The workflow needs no secrets or tokens. `permissions: contents: write`
in the file is what lets it create the release.

## Layout

`nsgm/` at the root is the package, and its name is the import name:
`import nsgm` works because the folder is called `nsgm`. That folder
cannot be flattened away — losing it would rename the package after
whatever directory replaced it.

A `src/nsgm/` arrangement is also common and equally correct; it keeps
the importable code apart from the packaging files. Either works. What
does not work is putting the modules directly in a `src/` folder, since
the package would then be called `src` and would collide with every other
project that did the same on install.

If you do move it, these are the files that have to agree:

| File | What it points at |
| --- | --- |
| `pyproject.toml` | `packages`, and `package-dir` if the code is nested |
| `nsgm.spec` | `pathex` and the `datas` source path |
| `run.sh`, `run.bat` | whether `PYTHONPATH` needs setting to import it |
| `run.bat` | the portable runtime's `._pth` line |
| `.github/workflows/build.yml` | the pre-build import check |

The interface files under `nsgm/web/` are read at runtime and must be
committed. Inside a built binary they land at `nsgm/web`, which is where
the server looks, so that lookup does not change with the layout.

## What to put in the repository

Everything here except `dist/`, `build/` and `runtime/`, which
`.gitignore` already excludes. The interface files under `nsgm/web/` are
required at runtime and must be committed.

## Installing it as a command

```
pip install .
nsgm
```

This puts `nsgm` on the path, which suits anyone comfortable with Python.
The built binary suits everyone else.

## A note on the licence

`LICENSE` currently says MIT, which lets anyone use, change and
redistribute the code as long as the notice stays. It is a placeholder
choice: it is your project, and swapping it for GPL, Apache-2.0 or
anything else is a matter of replacing that file before you publish.

## Warnings people may see

A binary downloaded from the internet is unsigned, so Windows SmartScreen
shows "Windows protected your PC" the first time, and macOS refuses to
open it until it is allowed in System Settings. This is normal for
unsigned software and does not indicate a problem with the build. Signing
requires a paid certificate from a certificate authority; without one,
the warning is unavoidable and worth mentioning in your README so people
are not alarmed.
