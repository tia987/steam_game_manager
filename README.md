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

## What's out now

The third tab is a reader for game deal, giveaway and release feeds. It
shows headlines and links only. Nothing is downloaded, and every entry
opens on the original site in a normal browser tab.

Entries are merged from every enabled feed, sorted newest first, and
filtered by the tag chips along the top (Free, Deals, Sales, News).
Anything naming a game you already have is marked **In your library**,
matched against both your scan results and your existing Steam
shortcuts — useful for skipping past a sale on something you own.

It ships with these, and you can enable, disable or delete any of them:

| Feed | Covers |
| --- | --- |
| itch.io sales, free games, new releases | itch.io's own official feeds |
| r/GameDeals, r/FreeGameFindings | community-posted deals and giveaways |
| Free games, Fanatical, IndieGala | Steam group announcement feeds |
| Steam news | storefront news |

**Adding your own.** Press *Manage feeds* and paste either the feed
address or just the site — `indiegames.example` works as well as
`indiegames.example/feed.xml`. Pressing *Find* will:

1. check whether what you pasted is already a feed
2. if not, read the page and look for the feed it advertises
3. if the page advertises none, try the addresses sites commonly use
   (`/feed`, `/rss`, `/feed.xml`, `/atom.xml` and a few more)

Whatever it finds is fetched and parsed before being offered, so you see
the feed's real title, how many entries it holds and the newest headline
**before** adding it. Nothing gets saved on the strength of a URL that
might not work.

RSS, Atom and RDF are all handled — the reader detects the format rather
than requiring a particular one. If a site genuinely has no feed you get
told where to look rather than a silent empty list.

**Tags.** Give each feed a tag and it becomes a filter chip along the top
of the tab. The built-in ones use Free, Deals, Sales and News, but the
field is free text, so a feed tagged `Indie` gets its own chip. Tags are
editable later from the same dialog, on any feed including the built-in
ones.

Feeds are cached to disk, so reopening the tab is instant and only
*Refresh* touches the network. Conditional requests mean unchanged feeds
cost almost nothing to re-check. If one feed is down or blocks the
request, it is reported by name underneath the toolbar and the others
still load.

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
HTML. A collection address walks the scrape API by cursor, which hands
over every item without page numbers being involved at all, so nothing
is missed and there is no depth to set. An item address reads its file
list directly and derives the magnet from its `_archive.torrent`.

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
checksums, signatures, debug symbols, dedicated server builds, and
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
