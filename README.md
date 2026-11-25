# Non-Steam Game Manager

Finds the games already installed on your computer, matches each one to
artwork on SteamGridDB, and writes them into Steam as non-Steam shortcuts
in a single pass. It does the same job for ROM folders that Steam ROM
Manager does, and it handles your Epic, GOG, Ubisoft, EA, Battle.net,
Xbox and itch libraries in the same run.

You review a wall of covers, click the ones you want, and press one
button. No copying paths, no dragging images into the grid folder.

It also carries a feed reader for game deals and giveaways, so you can
see what is going cheap or free right now without leaving the app.

- Runs entirely on your machine, on `127.0.0.1`
- Installs downloaded games into library folders you choose, by priority,
  with installers' own windows held inside the app
- No dependencies at all beyond Python 3.9 itself
- Backs up `shortcuts.vdf` before every write, keeping the last twelve
- Remembers what it added, so you can take it all back out later
- Has a full-screen store for a controller and a Moonlight stream:
  pick a game, press Install, and it ends up in Steam

## Getting it as one file

If you have a single `nonsteam-game-manager` file, run it and skip the
rest of this section: it carries its own Python and needs nothing
installed.

Building one yourself, or having GitHub build it for Windows, macOS and
Linux without owning those machines, is covered in `BUILDING.md`.

## Running it without installing anything

Double-click **run.bat** on Windows. It checks whether Python is already
on the machine, and if it is not, it offers to fetch a portable copy into
this folder — about 11 MB, straight from python.org.

That copy is not an installation:

- it lives only in the `runtime` folder next to this file
- nothing is written to your PATH or the registry
- no administrator rights are needed
- deleting the folder removes every trace of it

Say no at the prompt and nothing is downloaded; the script tells you where
to get Python yourself instead.

On Linux use **run.sh**. Python 3 is already present on nearly every
distribution, including SteamOS. If your unzip tool dropped the
executable bit, restore it with `chmod +x run.sh` or just run
`python3 -m nsgm` directly.

If you would rather install Python properly, `winget install
Python.Python.3.12` or the python.org installer both work — tick "Add
python.exe to PATH" on the first screen of the installer. The launcher
will find it and skip the portable copy.

**If the download is blocked** by a firewall or proxy, run.bat prints the
exact manual steps: download the embeddable zip, extract it to a folder
called `runtime`, add one line to its `._pth` file, and run again.

## Getting started

1. **Get a SteamGridDB API key.** Sign in at
   [steamgriddb.com](https://www.steamgriddb.com), open Preferences, then
   the API tab, and generate a key. It is free.

2. **Start the app.**

   ```
   python -m nsgm
   ```

   Run this from the folder containing `nsgm`. Your browser opens at
   `http://127.0.0.1:8731`. Leave the terminal window open while you work.

3. **Paste the key** into Settings, in the top right.

4. **Close Steam.** Steam holds `shortcuts.vdf` in memory and overwrites
   it when it exits, so anything added while it is running is lost. The
   badge in the header tells you the current state, and the app refuses
   to write while Steam is open.

5. **Pick your sources, scan, find covers, add.**

## The three kinds of source

**Launchers** are detected automatically. Tick the ones you use in the
left column. Nothing is read except the launcher's own installation
records.

| Launcher | Where the list comes from |
| --- | --- |
| Epic Games | `ProgramData\Epic\...\Manifests\*.item` |
| GOG Galaxy | `HKLM\SOFTWARE\WOW6432Node\GOG.com\Games` |
| Ubisoft Connect | `HKLM\...\Ubisoft\Launcher\Installs` |
| EA | `ProgramData\Origin\LocalContent\**\*.mfst` |
| Battle.net | Uninstall entries published by Blizzard |
| Xbox / Game Pass | `<drive>:\XboxGames\*\Content` |
| itch.io | `butler.db` |
| Heroic, Lutris, Flatpak, desktop menu | Linux equivalents of the above |

GOG registers each DLC pack as its own entry pointing at the game it
extends, so a game with five add-ons would otherwise fill the shelf with
near-duplicates. Those are skipped and counted in the scan notes.
Entries whose install folder no longer exists are skipped too. Hovering
any cover shows the folder it was found in, which is the quickest way to
check a source that looks wrong.

*Installed programs* is also offered on Windows. It lists everything in
Add/Remove Programs, which catches DRM-free games that no launcher knows
about, but it also catches ordinary applications. It is off by default;
use the filter box to find what you want.

**Game folders** are for anything installed by hand. Point at a folder
like `D:\Games` and every subfolder becomes one entry. The executable is
chosen by scoring candidates against the folder name and skipping the
usual noise: uninstallers, redistributables, crash handlers and
anti-cheat services.

**ROM systems** are one entry per console. Each needs a folder, a list of
extensions and an emulator command.

## ROM systems in detail

Pick a preset from the dropdown to fill in the extensions and, if you
point at RetroArch first, the right core. Then adjust as needed.

Available in the launch arguments:

| Token | Becomes |
| --- | --- |
| `{rom}` | full path to the ROM |
| `{romName}` | file name with extension |
| `{romBase}` | file name without extension |
| `{romDir}` | folder holding the ROM |
| `{emuDir}` | folder holding the emulator |

A RetroArch entry looks like this:

```
Emulator   C:\RetroArch\retroarch.exe
Arguments  -L "C:\RetroArch\cores\snes9x_libretro.dll" "{rom}"
```

A standalone emulator is usually simpler:

```
Emulator   C:\Emulators\PCSX2\pcsx2-qt.exe
Arguments  -batch "{rom}"
```

Titles are cleaned on the way in, so `Legend of Zelda, The - A Link to
the Past (USA) [!].sfc` reaches Steam as *The Legend of Zelda - A Link to
the Past*. Multi-disc sets collapse into one entry, preferring an `.m3u`
playlist when one sits beside the discs, otherwise the first disc.

## Choosing artwork

**Find covers** looks up everything selected and pulls the top-rated
cover for each. Covers appear on the wall; anything unmatched keeps a
typographic placeholder and a dashed edge so it is easy to spot.

To change one, hover a card and press **Art**. Search by a different
name if the automatic match was wrong, then pick from the alternatives.
Each artwork type has its own tab:

| Type | Size | Where Steam shows it |
| --- | --- | --- |
| Cover | 600×900 | the library grid |
| Wide capsule | 920×430 | lists and the Recent row |
| Hero | 1920×620 | banner on the game's page |
| Logo | varies | over the hero banner |
| Icon | small | shortcuts and the friends list |

Files are written to `userdata/<account>/config/grid` using the
identifier Steam derives from the shortcut, so the client finds them
without any further help.

## Get software

Paste a project's download page and it lists the files, ready to fetch.
Type a name instead and it looks for matching projects. Release pages on
GitHub, Codeberg and SourceForge are read through their APIs, which is
exact; anywhere else the page itself is parsed.

Results split into **Runs on this computer** and **Other builds**, and
chips filter by kind — everything, direct downloads, or magnets and
torrents — with counts. A text box filters by file name.

Magnets and torrents are never dropped to fit the result cap. They sort
last, being unrunnable directly, so a flat cut used to delete them on
deep crawls where merged pages of installers filled every slot. They are
few by nature and are now kept whole, with the cap spent on files and
growing with how much was read.

**Torrent directories.** Most sites that distribute over BitTorrent link
a `.torrent` file and never print the magnet. Rather than reporting no
magnets, the `.torrent` is read and its magnet worked out: the info hash
is the SHA-1 of the file's own info section, which is exactly how every
torrent client derives it. So a magnet is offered wherever a torrent is
found, whether or not the site spelled one out.

**The Internet Archive** is read through its own APIs instead of its
HTML, which its pages draw with script. A collection or search address
lists one magnet per item, built from the torrent hash the Archive's
search hands over, so thousands of items cost no extra requests. The
first look reads a thousand items; *Scan the whole site* reads up to
twenty thousand. Items the Archive does not seed are counted and left
out. **Files** beside an item lists that item's own files, read from its
file list, with the magnet derived from its `_archive.torrent`.

**Blogs and directories.** Many sites list games as blog posts,
WordPress being the most common: every post at the top of the site, next
to About and Contact, so the shape of an address says nothing. Post
titles carry a marker blog software adds (`rel="bookmark"`), and when a
page has several, *Treat as a catalogue* takes those posts and nothing
else, following the older-posts pages. A post that only describes a game
and links to its official site or forge has no files of its own;
opening it follows that link once, skipping social and share links, and
lists what the project itself offers. The note above the results says
when that happened.

**Files that come in pieces.** An installer split into volumes
(`setup.exe` with `setup-1.bin`, `setup-2.bin`) and a CD image (`game.cue`
with `game.bin` or `game (Track 01).bin`) are shown as one entry, marked
with how many parts it has. Downloading it fetches the parts too, into
the same folder, where the installer or emulator looks for them. A
`.bin` that belongs to nothing else stays listed on its own.

**Progress while crawling.** A crawl has no knowable length until it
ends, because every page read can reveal more. Rather than showing a
fraction of a total that keeps being revised upward, it shows what is
actually known: pages read, how many are still queued, and what has been
found. The addresses fetched are listed in the job log, so an unexpected
page count can be looked at rather than guessed about.

**Counting pages once.** Sites link the same page several ways: with and
without a trailing slash, with a sort order tacked on, with a tracking
parameter. Pages are identified by which listing and which number they
are, so all of those count as one page and are fetched once.

**Naming magnets.** A magnet URI can carry a `dn=` display name, and that
is used when present. When it is missing, the name comes from the title link
in the magnet's own row rather than from the magnet's anchor text, which on a
torrent listing is almost always a bare label like "Magnet" or a
site-branded variant of one. Only a magnet with no name anywhere falls
back, and it says so plainly instead of showing a hash.

**Magnet links.** Some projects publish over BitTorrent rather than plain
HTTP. A magnet is a reference, not a file, so there is nothing to fetch
directly. The app has no BitTorrent engine of its own, so it hands them to
a torrent client:

- **qBittorrent or Transmission, connected in Settings.** The magnet (or
  `.torrent`) is added through the client's own web API, saved into your
  finished-downloads folder, and its progress shows under **Installs**.
  The client can stay in the background with its window closed. For
  qBittorrent, switch on its Web UI (Tools → Options → Web UI); ticking
  "bypass authentication for clients on localhost" there makes the
  username and password unnecessary. **Test** in Settings checks the
  connection.
- **Otherwise,** whatever app is registered for magnet links, exactly as
  clicking the link in a browser would.

**Checksums.** Any SHA-256 printed on the page is collected. When a
download finishes its own digest is compared against them, and you are
told plainly whether it matches. A mismatch means stop.

**Listings split over several pages.** *Scan the whole site* follows a
listing to its end. There is no page number to choose: it stops when the
site stops offering another page, and reports progress as it goes.
Pagination is followed by `rel="next"`, by numbered page links, and — only
where a page publishes no links at all because its controls are drawn by
script — by inferring `?page=2`. A site that lists seventeen pages is read
in seventeen, not padded out with invented addresses.

Magnets and torrents found along the way are all kept. Nothing is trimmed
to a round number, and if a ceiling is ever reached the results say so
rather than leaving you to wonder whether a suspiciously neat total is
real.

Under the file list is a line saying what the crawl saw: pages read,
links to other pages, file links, and whether the page leans on script.
*Scan deeper* triples the budget and fetches again.

**Older versions.** When a project has a real release history, a
**Release** picker appears beside its files.

A full scan is kept for a week, an ordinary look for half an hour, and
both under one entry per address. Reopening the app, or pressing Find on
an address already scanned in full, reuses that scan rather than starting
again from shallow. A quick look never overwrites a full scan of the same
address. **Refresh** ignores all of it and fetches again.

### Collecting a whole catalogue

*Treat as a catalogue* is a separate, deliberate action for sites that
index hundreds of programs across many pages. It collects the entries
rather than the files, into a list you can search locally, and clicking
one opens that program's downloads.

*Leave catalogue* returns you to whatever you were looking at.

It is opt-in on purpose. Working out automatically whether a page is one
program or a thousand is guesswork, and guessing wrong takes away the
download view you actually wanted. Tags, categories and sidebars are separated from
entries by counting how many pages each link appears on: a tag sits on
every page of a listing, a program on exactly one. That works whatever a
site calls its URLs, which a list of known path names did not.

## Finished downloads

Point the sidebar panel at wherever your torrent client saves and it
reports what has landed and what each item still needs:

| State | Meaning |
| --- | --- |
| ready | a runnable folder; can go straight into Steam |
| packed | an archive, with an Unpack button beside it |
| installer | a setup program that needs running |
| disc image | an .iso or similar, for mounting or an emulator |
| unclear | nothing runnable found; worth a look |

**Watch for finished downloads** checks the folder every fifteen seconds.
A torrent client gives no signal when it finishes, so completion is
inferred from the folder going quiet: no part-files left underneath, and
a size that has not moved since the previous look. Each item is announced
once, when it settles.

**Watching for finished downloads** happens in the app itself, every
fifteen seconds, whether or not a page is open. When something finishes
you get a note with an **Install** button; with automatic installing on
(see *Installing games*) it is installed without asking.

**Stage ready items** puts them into the same list as a normal scan, so
artwork matching and adding to Steam work exactly as they do for anything
else. Unpacking refuses archives that try to write outside their own
folder, including through links.

## Installing games

The **Installs** tab is where downloads become installed games. Press
**Install** beside a finished download, on a file in the download dialog,
or use **Install a file…** for anything on disk: installers, archives
(zip, 7z, rar, multi-part sets), game folders, single programs, AppImages
and macOS disk images.

### Where games go

Add your **library folders** in priority order. A game goes into the first
folder with room for it, after leaving that folder's reserve free (10 GB
unless you change it). So a big hard disk first and a smaller SSD second
means games land on the hard disk, and anything that does not fit there
goes on the SSD.

Each folder shows its free space and whether it is on an SSD or a hard
disk. That is detected — Windows is asked whether the drive "incurs a seek
penalty", the same test its defragmenter uses; Linux and macOS report it
directly — and can be overridden per folder when detection gets it wrong
(some USB enclosures and RAID setups hide it). Library folders are also
included in every scan, so installed games stay on the shelf.

**How big will it be?** An installer does not say. The game's Steam store
entry usually does ("Storage: 70 GB available space"), so the title is
looked up there, carefully: a sequel shares most of its name, and "Hades"
picking up the requirements of "Hades II" would be worse than finding
nothing. Without a confident match the estimate comes from the download
itself — exact for zip and tar archives, twice the installer's size
otherwise, deliberately on the generous side. You can always pick the
folder yourself in the install sheet.

### Games that need an SSD

Some recent games state that they require an SSD; on a hard disk they
stutter, load for minutes or stream textures in late. The same Steam
requirements say so ("Additional Notes: SSD required"), and the minimum
spec is what counts: "SSD recommended", "SSD (preferred)" or "HDD
supported" mean the game still runs from a hard disk.

What happens with a game that *requires* one is a setting:

| Setting | What it does |
| --- | --- |
| **Only on an SSD** (default) | Only SSD folders are considered, ignoring the order. If none has room, the game waits under Installs with the reason, rather than going somewhere it will run badly. Choose a folder by hand to override. |
| An SSD first | SSD folders first, then the rest, with a warning if it lands on a hard disk. |
| Like any other game | The normal order applies. |

Games that only *recommend* an SSD follow your normal order, unless you
tick the option to put those on an SSD first too. The install sheet shows
what was found and lets you override it per game ("Needs an SSD" / "Any
drive is fine").

### The installer's own screens, inside the app

When you press Install, an installer can run two ways:

- **Its own screens, inside this window** (the default). The installer's
  wizard is held inside the app: its title bar and frame come off and it
  sits in a panel, so nothing pops out onto the desktop. Where the
  installer's maker is known, the library folder is pre-filled in its own
  folder page. **Hide** tucks it away while it works, **Pop out** gives it
  back its own window if anything misbehaves, and closing the app lets go
  of it first, so a running installer is never destroyed with the window.
- **Quietly, into the library folder.** No screens at all; progress shows
  under Installs. Available when the installer's maker is recognised:
  Inno Setup (GOG offline installers and most repacks), NSIS, Windows
  Installer (`.msi`), InstallShield, WiX bundles and Advanced Installer.
  Anything else is never guessed at.

**Administrator rights.** Most game installers ask for them. Windows only
lets one program hold another's window when both run with the same rights,
so an installer that asks opens in its own window, and the panel says so.
Ticking **Start with administrator rights** in Settings (or the button in
that panel) restarts the app with them: one Windows prompt per start, and
installers then stay inside. Unattended installs need it too, since
Windows' permission prompt cannot be answered by anything but a person.

Uninstallers are held the same way, and **Uninstall** offers to remove a
program quietly when its maker's quiet switch is known.

### Automatic installing

**Install new downloads automatically** is off by default. Switched on,
downloads that finish in your finished-downloads folder — or through the
app's own downloader — install quietly into your library, one at a time.
Downloads already in the folder when you switch it on are left alone and
offered as one button instead. A download that finished while the app was
closed is still picked up next time it starts.

Anything that cannot be installed unattended waits under Installs with the
reason and a button for what it needs: an installer whose maker is not
recognised waits for you to go through its own screens, a game with
nowhere to go waits for a folder, an archive that needs 7-Zip waits for
it.

After a successful install the game is put on the Add games shelf,
selected and ready for covers. Two more options: **Delete the download
once it has installed** (only ever inside your download folders), and
**Add installed games to Steam straight away** (when Steam is closed;
otherwise it waits on the shelf).

### 7-Zip

`.7z` and `.rar` archives need 7-Zip. The app looks where 7-Zip normally
installs, not only on the PATH (its installer does not add itself there).
If there is none, on Windows it offers to fetch the official build from
7-zip.org into its own folder: the 7-Zip installer is itself an archive,
so the small standalone `7zr.exe` unpacks `7z.exe` and `7z.dll` out of it
and nothing is installed. On Linux and macOS, install p7zip or 7-Zip with
your package manager; `bsdtar` is used when it is there.

## Watched pages

Press *Watch this page* and it joins a list you can re-check on demand.
What gets fingerprinted is the set of download links, not the page, so a
site that reshuffles its banners hourly stays quiet while a genuine
version bump speaks up. The first check records silently — otherwise
everything would look like news on day one.

This is also the answer for pages the resolver cannot read. It still
notices when the offered files change; you click through only when there
is a reason to.

## Taking games back out

The **Already added** tab lists what this tool created and nothing else,
so your hand-made shortcuts are never touched. Select any and remove
them; their artwork goes too.

## The store

A full-screen front end for the couch, built for a controller and for
streaming the PC to a television with Moonlight. Browse covers, search,
press **Install**, and the game is downloaded, installed into your
library folder and added to Steam, artwork included, without touching a
mouse.

### Opening it

- **From Steam.** Settings → *Add to Steam* puts a **Game Store** tile in
  your library, with its own artwork, so it opens from Big Picture like
  any game.
- **From Sunshine, as its own Moonlight app.** In Sunshine's web page,
  Applications → Add New, put `nonsteam-game-manager --store --streaming`
  (the store's Settings → *Moonlight and Sunshine* shows the exact line
  for your PC) in **Command**, not in Detached Commands: Sunshine starts
  detached commands and forgets them, so quitting from Moonlight would
  leave the store running. Turn on the app's **elevated** (run as
  administrator) option there too: Sunshine, running as a service, then
  starts the store with administrator rights itself, without a Windows
  prompt. The store never asks for the rights itself while streaming,
  even with *Run the store as administrator* on, because the prompt would
  take over the stream and leave the store without input. Running as
  administrator can also cut the browser engine off from controllers (it
  reads Xbox-type pads through Windows.Gaming.Input, which does not serve
  elevated programs, and may keep listing a pad that never moves), so the
  store reads them itself and always combines that with what the engine
  reports: Xbox-type pads through XInput, and PlayStation pads through
  Windows' joystick functions (WinMM). The second matters over Moonlight:
  with a PlayStation controller in your hand, Sunshine's automatic setting
  emulates a DualShock 4, which XInput cannot see. In a stream the store
  always listens; on the desktop only while it is the window in front, so
  a game started from it does not drive the store too. While streamed it
  also puts itself back in front whenever only the desktop or taskbar is,
  for the phone keyboard and touches. On a stream it says once, a few
  seconds after opening, which controllers it reads itself; the same
  details (administrator or not, window in front, pads seen, whether the
  page receives them) go to `input.log` in the config folder whenever they
  change, and to Settings → Controller. Then:
  - **Leave** closes the store completely, which ends that stream, so you
    can start your Steam app in Moonlight right after. Quitting the app
    from Moonlight's menu closes the store as well.
  - **Disconnecting** is different. L1 + R1 + Select + Start, or closing
    Moonlight, only disconnects: Sunshine keeps the app running so the
    stream can be resumed, runs no Undo command, and tells nobody but its
    log. So while streamed, the store reads the end of Sunshine's log
    (`C:\Program Files\Sunshine\config\sunshine.log`, Apollo's too, or
    the file named by `SUNSHINE_LOG` or the `sunshine_log` setting). If
    the log says `CLIENT DISCONNECTED` and the client has not reconnected
    within 10 seconds, the store closes, which also ends Sunshine's
    session. Settings → *When Moonlight disconnects*: **Close after
    downloads finish** (the default: closes at once when nothing is
    running, otherwise once downloads and installs are done, so an
    installer is never cut off halfway), **Close straight away**
    (downloads pick up where they stopped next time) or **Keep running**.
    Games waiting for a Steam restart get it on the way out, with
    *Restart Steam: When I leave the store* chosen.
  - **`--quit-store`** closes a running store from outside and exits:
    `nonsteam-game-manager --quit-store` (Settings shows the exact line).
    It asks the store to close the same way its Leave button does, waits
    up to 10 seconds, and only then ends the process. A game manager
    window that is not the store is left alone. Use it as the Game Store
    app's Undo command in Sunshine, as a client's disconnect command in
    Apollo, or from any script. Each running copy leaves a note (port and
    token) in the config folder's `running` folder for it; if there is
    none it looks on the usual ports.
  - The store is not something Steam launched, so when new games need
    Steam restarted it restarts Steam quietly in the background and stays
    open, instead of closing and reopening itself mid-stream.
  - With *run as administrator* on, the store relaunches itself elevated
    as usual, but the first copy waits for it, so Sunshine still sees one
    program running; and the elevated copy closes if the first one is
    ended from Moonlight.
  - Sunshine's own `SUNSHINE_APP_*` variables are recognised too, so
    `--streaming` is only a belt and braces.
- **From the command line.** `python -m nsgm --store` opens it full
  screen; add `--windowed` for a normal window.
- **From the main app.** The **Store** button in the header.

### Sorting

Discover lists the newest releases first: by the date a release website
posted them (or a JSON catalogue's optional `date`, `YYYY-MM-DD`), then
in the site's own order. Games without a date follow, A to Z. The first
chip in the row switches between *Newest first* and *A to Z*, and is
remembered.

### Controls

| Xbox | PlayStation | Does |
| --- | --- | --- |
| D-pad, left stick | D-pad, left stick | move |
| A | ✕ | open, press |
| B | ○ | back; on Discover, leave the store |
| X | □ | search, with an on-screen keyboard |
| Y | △ | Downloads |
| LB / RB | L1 / R1 | previous / next tab |
| Menu (☰) | Options | Settings |
| View (⧉) | Share / Create | leave the store |
| Right stick | Right stick | scroll |

The button icons are PlayStation 5's by default, drawn the way the
DualSense prints them: light grey ✕ ○ □ △ on dark buttons, Options and
Create as their icons, L1/R1 as dark keys. Over Moonlight, Sunshine
usually presents the pad to Windows as an Xbox one whatever is in your
hands, so following the controller would show Xbox letters; Settings →
Controller switches to Xbox letters, or to *auto* (follow the
controller, with the coloured shapes for a DualShock 4).

Moonlight's virtual controller, an Xbox or PlayStation pad, and Steam
Input all work. If Steam Input maps the controller to keys instead,
arrows, Enter and Escape do the same jobs, and a mouse or keyboard works
throughout.

### Getting games into Steam while Steam is open

Steam reads `shortcuts.vdf` only when it starts and overwrites it when it
quits, so a shortcut written while it runs is lost. While you are
streaming, Steam is running. The store therefore keeps finished games in
a waiting list, shown at the top right, and restarts Steam to add them.
When that happens is a setting:

| Setting | What happens |
| --- | --- |
| **When I leave the store** (default) | Leaving asks whether to restart Steam now. Say no and the games are added the next time Steam is closed. |
| **As soon as installs finish** | Once nothing is downloading or installing, Steam restarts by itself and the store comes back afterwards. |

The restart is done by a small helper process that outlives both the
store and Steam: it asks Steam to quit cleanly, writes the waiting
shortcuts, and starts Steam again in Big Picture (or the normal window,
also a setting). Its log is `store-refresh.log` in the settings folder.
If Steam is closed when a game finishes, it is added straight away and
none of this is needed.

### Installing

The first install asks which drive games go on and makes a `Games`
folder there; after that the library folders from the **Installs** tab
are used, with the same SSD and free-space rules.

Installers run quietly, straight into the library folder, whenever
their maker is recognised: Inno Setup, NSIS, Windows Installer,
InstallShield, WiX and Advanced Installer, which between them cover
nearly every game installer. Running quietly means passing the
installer the switches its maker defined for it, and an installer of
unknown make has no such switches to pass, so it is never guessed at.

Those show their own screens, held inside the store, and the controller
works them the way a keyboard would:

| Xbox | PlayStation | In the installer |
| --- | --- | --- |
| D-pad ↑ ↓ | D-pad ↑ ↓ | previous / next control (Shift+Tab / Tab) |
| D-pad ← → | D-pad ← → | arrow keys |
| Left stick | Left stick | move inside a list, such as a list of options to tick |
| A | ✕ | press the selected button, tick the selected box (Space) |
| Menu | Options | the default button: Next, Install, Finish (Enter) |
| X | □ | type into the selected text box, such as the install folder |
| B | ○ | hide the installer; it keeps running |
| View | Share / Create | pop it out into its own window |

What is selected is written above the installer in large type, since an
installer's own focus outline is hard to see on a television. Typing
replaces the text box's contents, and on the keyboard Shift + `/` gives
`\`. This needs the installer to be held in the store, which in turn
needs the app to have the same rights as the installer: if an installer
asks for administrator rights, turn on *Start with administrator rights*
in the main app's Settings.

**Only the game.** Releases often carry more than the game: patches and
updates that only apply on top of an install, hotfixes, DLC, soundtracks,
demos, language packs, editors. None of those are ever chosen. When the
newest release holds nothing but a patch, the store goes back to the
newest release that has the full game, and pre-releases are skipped. A
catalogue's direct link that points at one of those is refused before
anything is installed, with the reason shown. Downloads go to
`Downloads/Game Store downloads` and are deleted once installed, unless
*Keep downloaded files* is on.

**After the game is in place**, repack installers like to open more
windows, which a controller over Moonlight cannot click. The store deals
with each (Windows):

- **Extras.** DirectX, Visual C++, .NET, PhysX, OpenAL and similar setups
  the installer starts are stopped as soon as they appear, unless
  *Let installers add DirectX, Visual C++ and other extras* is on. Most
  PCs have them already. A game's page has the same switch for that game,
  set before pressing Install.
- **The file checker** (QuickSFV and similar, the red-and-white lifebuoy)
  is always stopped: it ends on a window that wants a click. *Check the
  files after installing*, in Settings or on a game's page, does the same
  check itself against the release's `.md5`/`.sfv` list, quietly, and the
  result is shown when the install finishes.
- **The installer's last page.** Its ticked boxes (launch the game,
  visit the repacker's website, read me) run only when Finish is pressed,
  and the installation is complete by then. So when an Inno Setup or NSIS
  installer reaches that page, the store closes it: nothing there runs,
  and nothing needs clicking.
- **Desktop shortcuts** the installer adds are removed (*No desktop
  shortcuts*, on by default). Shortcuts that were already there are never
  touched.
- **"Run as administrator"** that installers tick on the game is taken off
  again (*Don't let games ask for administrator rights*, on by default),
  so the game starts without a Windows prompt. Other compatibility
  settings on it are kept.

**Paths the installer can read.** Many repacks unpack with an old library
(ISDone.dll with unarc.dll) that cannot open a file whose path has
anything outside plain English letters; a "–" from the title is enough
for *Unarc.dll returned an error code: -6 … can't open fg-01.bin* (the
`.bin` files are the game's packed data). So the store names download and
install folders in plain ASCII, short (*Aqueduct 117 Civitas Nova - Gold
Edition*), carries a download made under the old naming over instead of
fetching it again, and starts the setup program through Windows' short
8.3 path when something above it, such as a user name with an accent, is
not plain. It also lets the built-in downloader close a finished torrent
before the installer starts, and waits until every data file can be
opened (a virus scanner can hold a fresh file for a moment).

Repack installers run as administrator, and Windows only lets a program
with the same rights stop their windows. Turn on *Run the store as
administrator*: one prompt when the store opens, instead of one per game,
and the installers' screens can be held inside the store too.

*Mute installers* (on by default, Windows) silences the music many
repack installers play while they unpack: each of the setup program's
own processes is muted in the Windows volume mixer as soon as it starts
making sound, the same switch the mixer's per-app slider has. Programs
inside the game's own folder are never muted, so a game launched at the
end of a setup keeps its sound, and anything still running when the
install ends is unmuted again. Nothing else, and not the system volume,
is touched. An interrupted download carries on where
it stopped.

### Many downloads at once

Pressing Install on dozens of games is fine:

- **Direct downloads take turns**, two at a time by default (Settings →
  *Direct downloads at once*, up to 4). The rest show *Waiting its turn*.
  Thirty downloads from the same file hosts at once only get throttled.
- **Torrents queue in the torrent app**, under its own limit on active
  downloads; the store shows *Queued in qBittorrent* and waits.
- **Disk space is set aside** before each download starts: its download
  size, plus room to unpack it, counted against what every other running
  download still has to write, with 2 GB always left free. One that would
  not fit says so instead of filling the disk half way through.
- **Unpacking happens one game at a time**, since it is all disk work.

### Choosing the drive

Settings → *Installing* lists your library folders with each drive's
type and free space, all workable with the controller:

- **Use first** puts a drive at the top; games go to the first folder
  with room for them (and an SSD, for games marked as needing one).
- **Switch off** stops new games going there; **Remove** takes it out of
  the list. Neither deletes anything; games already there stay.
- **Add another drive** offers every drive that has no library folder
  yet; a `Games` folder is made on the one you pick.
- **Install to…** on a game's page picks the drive for that one game.
  Turn on *Ask which drive for each game* to be asked every time you
  press Install.

### Downloading over BitTorrent

Settings → *BitTorrent* chooses how torrents are downloaded.

**Built-in downloader** (the default) needs nothing else open and
nothing set up, which is the point when all you have is a controller
over Moonlight. It is aria2, a long-established open-source downloader,
fetched once on first use (2.5 MB, from aria2's own GitHub release, and
checked against a fixed SHA-256 before it is ever run; on Linux and
macOS install `aria2` with your package manager). It runs hidden, only
while the app runs, and takes orders on 127.0.0.1 only, behind a random
password. It **downloads and stops**: each torrent is finished the
moment it is complete, with no seeding afterwards. While downloading,
BitTorrent always sends some pieces to others (that is how the protocol
works); that is held to 100 KB/s. Three torrents download at once and the
rest queue. Optional files are left out before anything downloads.
Windows may ask once whether aria2 may accept connections from the
network; downloads work either way, so that question can simply be
closed.

**My torrent app** uses Transmission or qBittorrent running on the same
PC instead, and drives it the whole way so you never open it:

1. **Finding it.** If no torrent client is set up, the store looks for
   one on its usual port (Transmission 9091, qBittorrent 8080) and keeps
   the one it finds. Settings → *BitTorrent* shows which, and *Look for
   it* checks again. The one thing it cannot do is switch on the
   client's remote access, which each client keeps off until you turn it
   on once: Transmission under Edit → Preferences → Remote → *Allow
   remote access*, qBittorrent under Options → Web UI (tick *Bypass
   authentication for clients on localhost* to skip the password). If it
   wants a password, **Sign in** asks for the username and password with
   the on-screen keyboard (the password shows as dots) and checks them
   before keeping them.
2. **Downloading.** The torrent is added with the store's download folder
   as its destination. Progress, speed and time left show in the store
   like any other download. Cancelling removes it from the client along
   with its partial files.
3. **Installing.** When it completes, the game is chosen out of what
   arrived (the full game's installer or folder, never a bundled patch,
   update or DLC installer) and installed as usual.
4. **Tidying up.** The torrent is removed from the client and the
   downloaded files deleted. Turn on *Keep sharing after installing* to
   leave it seeding instead.

Torrents the store adds are tagged *Game Store* in the client, and the
store remembers them in `store.json`, across restarts. Only those are
ever removed or have their files deleted. If a torrent was already in
the client before you pressed Install, the store waits for it and
installs from it, but never removes it, deletes its files or changes
which of its files are downloaded. If the torrent app stops answering
for up to three minutes (restarting, busy) downloads wait for it rather
than failing. With Transmission, a tracker being down is not treated as
an error; only a local one such as a full disk is.

### Uninstalling

**Uninstall** is on a game's page and on each Library row. One
confirmation, then:

- If the game installed itself into Programs and Features, its own
  uninstaller runs, quietly where its maker allows, otherwise held in the
  store and worked with the controller like an installer. The entry is
  matched by where it is installed, never by name.
- Its folder is deleted. Only a folder inside one of your library
  folders is ever deleted, never a library folder itself or anything
  reached through a link; anything else is reported and left.
- It leaves Steam with its artwork. With Steam open, that waits for the
  next Steam restart, alongside any new games, under the same setting.

Saved games are not touched. Most games keep them in your user folders
(Documents, AppData, Saved Games), not in the install folder.

### Catalogues

The open-source catalogue is built in and on by default; switch it off to
hide its games. Add your own under Settings → Catalogues, in the store,
or under **Sources** in the main app's Settings; both edit the same
list: paste a web address or a file path and press **Check**. The
file is read and you see its name, its size and a few of its games
before anything is saved. Catalogues on the web are re-read every six
hours, or when you press *Check for new games*. *Read everything again*
reads every catalogue from the start: JSON files are fetched again, and
release websites are re-read in the background, page by page, with the
shelf usable meanwhile. Every game is refreshed in place; once a whole
site has been read, games it no longer lists are removed. Artwork that
was not found before is looked for again.

The address must lead to the file itself, not to a page showing it.
Share and page links from GitHub, Gist, GitLab, Codeberg, Pastebin,
Google Drive and Dropbox are turned into the file's own address
automatically; for anywhere else, use the site's *Raw* or *Download*
link. A path copied with Windows' *Copy as path* works as it is, quotes
included.

To try it, add this example, which has two games that are not in the
built-in list:

```
https://github.com/tia987/steam_game_manager/blob/main/examples/catalogue.json
```

A catalogue is a JSON file:

```json
{
  "name": "My catalogue",
  "games": [
    {
      "name": "SuperTuxKart",
      "repo": "supertuxkart/stk-code",
      "tags": ["Racing"],
      "blurb": "Kart racing with power-ups."
    },
    {
      "name": "Some Game",
      "downloads": {
        "windows": "https://example.org/some-game-win64.zip",
        "linux": "https://example.org/some-game.AppImage"
      },
      "cover": "https://example.org/cover-600x900.png",
      "size": 1500000000
    }
  ]
}
```

A bare list of games works too. Per game:

| Field | Meaning |
| --- | --- |
| `name` | required |
| `repo` | a GitHub `owner/name`: the latest release, with the file chosen for this computer as the Open source tab does |
| `asset` | with `repo`, a regular expression the file name must match, for projects that publish several builds |
| `downloads` | links keyed `windows`, `linux` or `mac`, or `any`: web addresses, `.torrent` addresses or magnet links |
| `download` | one link for every system; `magnet` and `torrent` are accepted as other names for it |
| `tags`, `blurb`, `licence`, `site` | shown on the game's page; tags become filter chips |
| `note`, `needs_data` | a warning shown in amber before installing |
| `cover`, `hero`, `logo`, `wide` | artwork for the store and for Steam; without them SteamGridDB is asked, if you have a key |
| `size` | bytes, used to pick a library folder before the download finishes |

An entry needs at least a `repo`, a download or a `site`. Entries with
none of those, or that repeat a name, are skipped and counted when you
press Check.

### Release websites as catalogues

A catalogue can also be a website that publishes one release per blog
post: a cover, a few `Key: value` lines, then a list of download mirrors
and usually a magnet link. Paste the site's front page (or a category
page) into the same box and press **Check**. Only the front page is read
for that, so it answers at once: you see its games and how many pages the
site has in all.

**How far back.** Settings → *Pages to read* sets it: 10 pages (about a
hundred releases) by default, stepping up to 5,000. *Read every page of
release websites* reads the whole site, however many pages it has.

**Read once, then only what is new.** A site is never read in full
twice, and the store never waits for it:

- The store opens with what is already kept on disk. Reading happens in
  the background, and the shelf grows as pages arrive; the catalogue's
  row in Settings shows how far it has got (*reading page 120 of 825*).
- The first time, the site is read a page at a time, gently (a pause
  between pages, and backing off when the site struggles). Progress is
  saved every ten pages, so closing the app loses nothing: the next start
  carries on from where it stopped. Raising the depth later also carries
  on from there instead of starting over. A whole site of 800-odd pages
  takes roughly a quarter of an hour, once.
- After that, at most every 30 minutes (or when you press *Check for new
  games*), the front page is looked at for new releases. Reading
  stops at the first release already on the shelf, so that is one
  request unless more than a page of releases came out since. New ones
  join the top of the shelf; releases on the pages read whose title
  changed (a new version) are updated.
- Each site is kept in its own file, and the shelf holds names, sizes and
  covers only: descriptions load when a game's page is opened, mirror
  links are read fresh when you install, and the grid draws tiles as you
  scroll. A shelf of thousands of games stays quick.

What is read from each post:

| From the post | Becomes |
| --- | --- |
| the title, e.g. `Game: Some Edition, v1.0.9 + Bonus OST` | the name `Game: Some Edition`, the version `v1.0.9` and the extras `+ Bonus OST`; artwork is looked up as `Game` |
| `Genres/Tags`, tag links | filter chips |
| `Company`, `Original Size`, `Repack Size` | shown on the game's page; the original size picks the library folder |
| the first picture | the cover, when SteamGridDB has none |
| a *Description* heading or spoiler | the blurb |
| each item under a *Download* / *Mirrors* heading | one mirror, with every file link inside it or in the spoiler after it |
| magnet and `.torrent` links | the BitTorrent route |

Posts with no download links on a page where other posts have them are
news ("Upcoming", "Updates digest") and are left out.

Installing one reads the post again, so the links are current, then:

1. **Picks a route.** Settings → *Games from release websites*: *Best
   available* uses BitTorrent when a torrent app is running and the
   mirrors otherwise, falling back to the other if the first fails;
   *Direct downloads* and *BitTorrent* use only that one. Over
   BitTorrent the release's optional files are unticked in the torrent
   app, by the same rule as below, and any scraps of them a client wrote
   anyway are removed before the setup program sees them. With mirrors to
   fall back on, a torrent whose details nobody shares within 15 minutes,
   or that makes no progress for 45, is removed with its partial files
   and the mirrors are used instead. Time spent queued or paused in the
   torrent app never counts. On *BitTorrent* only, it waits as long as it
   takes.
2. **Fetches every file the release needs**, into a folder of its own in
   the store's download folder. Files named `optional` or `bonus` are
   skipped unless *Download optional files* is on; of `selective-<language>`
   files only English is fetched (`store_language` in `settings.json`
   changes that). Each file is tried on the mirror that delivered the
   last one first, then on the others, so one host being down costs one
   retry, not the release. An interrupted file resumes, and files already
   finished are not fetched again.
3. **Gets past the file host's page.** Mirror links usually lead to a
   host's page with a button rather than to the file. The store follows
   it the way a browser would without running script: a link or quoted
   address naming the same file, or the page's download form, after any
   countdown, keeping the host's cookies. A host that builds its link
   entirely in script cannot be read; that mirror is reported and the
   next one tried.
4. **Unpacks before installing**, one game at a time. A release packed as an archive usually
   holds a setup program. It is unpacked first (7-Zip is fetched on
   Windows if needed), loose files such as language packs are put beside
   the setup program, and that folder goes to the install queue.

## Open source games

The **Open source** tab is a catalogue of games that are genuinely free
software: public source repository, recognised free licence, downloads
straight from the project's own release page. Filter by genre or search
by name.

Press **Find download** and the app asks that project for its latest
release, then picks the right file for your computer and hands it to the
downloader. From there it behaves like any other download: press
Install and it goes into your library, then onto the Add games shelf.

**Choosing the file** is fussier than it sounds, because release pages
carry a lot that is not the game. The picker refuses source archives,
checksums, signatures, debug symbols, dedicated server builds, patches,
updates, hotfixes, DLC, soundtracks, demos, language packs, and
anything built for a different processor — an ARM64 installer is never
handed to an x64 machine. Where a release contains nothing usable, the
card links to the release page instead of guessing.

**Engine reimplementations** are marked in amber. Projects like OpenMW,
OpenRCT2, CorsixTH and Julius are fully open source, but they replace
only the program: the artwork, sound and level data still come from the
original commercial game, which you need to own. Seven entries carry that
note, and it is shown before you download rather than after.

Two entries have no GitHub releases at all and link to their own sites:
Xonotic distributes from its homepage, and Space Station 14 installs
through its own launcher.

**About the GitHub limit.** GitHub allows 60 anonymous API requests an
hour per address, which a catalogue this size can exhaust in one sweep.
So results are cached for a day, and lookups happen one game at a time
when you ask for one, rather than all at once. If you do hit the limit
the app says so and tells you when it resets. A read-only GitHub token in
Settings raises the ceiling to 5000 an hour; it needs no scopes at all.

## Downloading

Feed entries that point straight at a file get a **Download** button.
Most do not: an r/GameDeals post links to a store page, not an installer,
so there is nothing behind it to fetch. Those entries just open on the
original site, which is where the purchase or claim happens anyway, and
that is the one thing deliberately left to your browser: it is where you
are signed in.

For anything that is a real file link, or for a link you paste yourself
through **Get from a link**, the app downloads it to a folder you choose
and shows progress. Then **Install** opens the install sheet, and **Show
in folder** lists the folder inside the app, with the file marked. With
automatic installing on, the download is installed straight away.

A few things it refuses, on purpose:

- **Web pages.** Checked twice, by content type and by the first bytes of
  the response, so a page served with a misleading file type is still
  caught. You get told to open it in a browser and copy the real link.
- **File names that would escape the folder.** A server claiming the file
  is called `../../../something.exe` gets it saved as `something.exe`
  inside the folder you picked.
- **Overwriting.** A second copy becomes `setup (2).exe` rather than
  replacing the first.

Interrupted downloads resume where they stopped if the server supports
it, which matters for anything game-sized.

## Uninstalling programs

The **Uninstall** tab lists everything Windows shows under Programs and
Features. Removing one runs that program's own uninstaller, held inside
the app like an installer, and you answer its questions exactly as normal.
Where its maker's quiet switch is known you can remove it quietly instead.
The app waits for every process the uninstaller starts, because some —
Inno Setup's among them — copy themselves elsewhere and exit at once.

The difference comes after. Uninstallers routinely leave save folders,
configuration directories and cached data behind, which is why "clean
removal" usually means a second manual sweep through AppData. When the
uninstaller finishes, the app looks for what it left and shows each
folder with its size.

Nothing is deleted unless you tick it. Three things make that safe:

- **Only related folders are proposed.** A publisher folder is never
  offered on its own, and only the subfolder belonging to the program you
  removed is proposed, so uninstalling one title by a studio never
  suggests deleting another.
- **A guard vets every path** before deletion. It refuses anything that
  is not strictly inside an application data folder, anything sitting near
  a drive root, any system or user folder, and anything reached through a
  junction or symlink — which would otherwise let a redirected folder
  delete something far away.
- **Refusals are shown, not hidden.** Anything the guard rejects appears
  greyed out with the reason.

Those folders often hold your saves. Read the list before ticking
anything, and if in doubt, leave it — you can always delete it later, but
not the other way round.

If the program also had a Steam shortcut this app created, removing it is
offered in the same step so the entry and its artwork go together.

## Where things are kept

| What | Where |
| --- | --- |
| Settings and the record of what was added | `%APPDATA%\NonSteamGameManager` on Windows, `~/.config/nonsteam-game-manager` elsewhere |
| Install queue and history | `installs.json` in the same folder |
| Store: what it installed, games waiting for Steam | `store.json`, with `store-catalogues.json` and `store-art.json` as caches |
| Store: each release website read so far | `store-site-<id>.json`, deleted when the catalogue is removed |
| Steam store look-ups (sizes, SSD needs) | `storeinfo.json`, kept 30 days |
| A fetched 7-Zip | `tools\7zip` in the same folder |
| Shortcuts | `<Steam>/userdata/<account>/config/shortcuts.vdf` |
| Backups | the same folder, named `shortcuts.vdf.<timestamp>.bak` |
| Artwork | `<Steam>/userdata/<account>/config/grid` |

To undo everything by hand, close Steam and rename the most recent backup
over `shortcuts.vdf`.

## Troubleshooting

**"Steam is running."** Close it fully, including the tray icon. On
Linux check with `pgrep -x steam`.

**Steam was not found.** Set the folder explicitly:

```
NSGM_STEAM_ROOT="D:\Steam" python -m nsgm
```

**Games appear but the covers do not.** Steam caches the library grid.
Restart it; if the art still does not show, confirm the files landed in
the `grid` folder and that the numbers match the `appid` in
`shortcuts.vdf`.

**A game launches its storefront instead of the game.** Untick *Skip
launchers, run the game directly* to route through the launcher, or tick
it to run the executable. Some titles need their storefront running and
will not start any other way.

**Nothing found in a game folder.** Check the depth: the scan expects one
game per subfolder. Point at `D:\Games`, not at `D:\Games\Hollow Knight`.

**Port already in use.** Pass another: `python -m nsgm --port 9000`.

**An installer opens in its own window.** Either it asked for
administrator rights (turn on *Start with administrator rights*), or the
app is in a browser tab rather than its own window, where there is no
window of the app's to hold it in.

**A game waits with "needs an SSD".** None of your SSD folders had room.
Free some space, add another SSD folder, choose a folder for it by hand,
or set it to "Any drive is fine" in the install sheet.

**The page says "Forbidden".** Every request carries a token the page
gets when it loads, so that other websites open in your browser cannot
drive the app. Reload the page; a page left open from before a restart
holds the old token.

**A feed shows an error.** Sites occasionally block automated requests or
move their feed. The message under the toolbar names the feed and the
reason; delete it in *Manage feeds* or replace it with a current address.

## How the identifier works

Steam derives a shortcut's id from its executable and name:

```
id = crc32('"C:\path\game.exe"' + 'Game Name') | 0x80000000
```

That id names the artwork files and is stored, as a signed 32-bit value,
in the `appid` field. This is why renaming a game or moving its
executable orphans its artwork: the id changes. Re-running a scan and
adding it again fixes it, and because the same inputs always give the
same id, re-adding a game updates its entry instead of duplicating it.
