/* Game Store - controller-first client.

   Everything here works three ways: a gamepad (read through the Gamepad
   API, which is what Moonlight's virtual controller shows up as), a
   keyboard (arrows, Enter, Escape - also what Steam Input sends when it
   maps a controller to keys), and a mouse or touch screen. Movement is
   spatial: pressing right goes to whatever is nearest to the right on
   screen, so no screen needs its own navigation map. */

"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN = (document.querySelector('meta[name="nsgm-token"]') || {}).content || "";
const TABS = ["discover", "library", "downloads", "settings"];
const ACTIVE = ["resolving", "downloading", "installing", "waiting", "adding", "uninstalling"];

const S = {
  data: null,
  games: [],
  byId: new Map(),
  tab: "discover",
  query: "",
  tag: "All",
  catalogue: "all",
  covers: {},
  coversRunning: false,
  jobs: {},
  installed: {},
  pending: [],
  pendingRemove: [],
  busy: false,
  embed: null,
  steamRunning: false,
  phases: {},
  detail: "",
  detailArt: {},
  detailSignature: "",
  memory: {},
  installerHidden: "",
  embedKey: "",
  checked: null,
  pollTimer: 0,
  lastFocus: null,
};

// ------------------------------------------------------------------ basics

async function api(path, body) {
  const headers = { "X-NSGM-Token": TOKEN };
  const options = body
    ? { method: "POST", headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : { headers };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick, key) {
  const node = make("button", `btn nav ${className || ""}`.trim(), label);
  node.type = "button";
  if (key) node.dataset.key = key;
  node.addEventListener("click", onClick);
  return node;
}

function toast(text, tone = "good") {
  const node = $("toast");
  node.textContent = text;
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

function human(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function hueOf(name) {
  let hue = 0;
  for (const ch of name) hue = (hue * 31 + ch.charCodeAt(0)) % 360;
  return hue;
}

function placeholder(game) {
  const box = make("div", "placeholder");
  box.style.setProperty("--hue", hueOf(game.name));
  box.append(make("span", "placeholder__name", game.name));
  if (game.tags && game.tags[0]) box.append(make("span", "placeholder__tag", game.tags[0]));
  return box;
}

function coverImage(url) {
  const img = make("img");
  img.alt = "";
  img.decoding = "async";
  img.addEventListener("load", () => img.classList.add("is-loaded"));
  img.addEventListener("error", () => img.remove());
  img.src = url;
  return img;
}

function plural(count, one, many) { return `${count} ${count === 1 ? one : many}`; }

// ------------------------------------------------------------------ state of one game

function gameState(game) {
  const job = S.jobs[game.id];
  const record = S.installed[game.id];
  if (job && ACTIVE.includes(job.phase)) {
    if (job.phase === "waiting") {
      return { kind: "waiting", label: "Needs you", message: job.message, job };
    }
    const label = job.phase === "downloading"
      ? (job.progress >= 0 ? `Downloading ${Math.round(job.progress * 100)}%` : "Downloading")
      : job.phase === "resolving" ? "Starting"
      : job.phase === "uninstalling" ? "Uninstalling"
      : job.phase === "adding" ? "Adding to Steam" : "Installing";
    return { kind: "busy", label, message: job.message, progress: job.progress, job };
  }
  if (record) {
    if (record.steam === "added") {
      return { kind: "installed", label: "Installed", message: "Installed and in your Steam library.", record };
    }
    if (record.steam === "pending") {
      return { kind: "pending", label: "After restart", record,
               message: "Installed. It appears in Steam once Steam restarts." };
    }
    return { kind: "installed", label: "Installed", record, warn: true,
             message: record.steam === "failed"
               ? `Installed, but not added to Steam: ${record.steam_error || "unknown error"}`
               : "Installed, but no program to start was found in its folder." };
  }
  if (job && job.phase === "failed") return { kind: "failed", label: "Failed", message: job.message, job };
  if (!game.installable) {
    return { kind: "off", label: "Not here",
             message: game.site
               ? `This one is only offered on its own site: ${game.site}`
               : "There is no download for this computer." };
  }
  return { kind: "available", label: "", message: job && job.phase === "cancelled" ? job.message : "" };
}

// ------------------------------------------------------------------ loading

async function loadOverview(refresh = false) {
  const data = await api(`/api/store${refresh ? "?refresh=1" : ""}`);
  applyOverview(data);
}

function applyOverview(data) {
  S.data = data;
  S.games = data.games || [];
  S.byId = new Map(S.games.map((g) => [g.id, g]));
  if (S.catalogue !== "all" && !data.catalogues.some((c) => c.id === S.catalogue)) S.catalogue = "all";
  applyStatus(data, true);
  paintGlyphs();
  renderChips();
  renderNotices();
  renderGrid();
  renderSettings();
  loadCovers();
}

function applyStatus(status, quiet = false) {
  S.jobs = status.jobs || {};
  S.installed = status.installed || {};
  S.pending = status.pending || [];
  S.pendingRemove = status.pending_remove || [];
  S.busy = !!status.busy;
  S.steamRunning = !!status.steam_running;
  noticeSync(status.sync || {});

  for (const [id, job] of Object.entries(S.jobs)) {
    const before = S.phases[id];
    if (!quiet && before && before !== job.phase) {
      if (job.phase === "done") toast(`${job.name}: ${job.message}`);
      else if (job.phase === "failed") toast(`${job.name}: ${job.message}`, "bad");
      else if (job.phase === "waiting") toast(`${job.name} needs you: ${job.message}`, "bad");
    }
    S.phases[id] = job.phase;
  }

  updateTiles();
  renderDetailState();
  renderPending();
  renderDownloadCount();
  if (S.tab === "library") renderLibrary();
  if (S.tab === "downloads") renderDownloads();
  renderInstaller(status.embed || null);
}

// A release website being read in the background grows the shelf. The
// grid is redrawn now and then while that happens, never while a game's
// page or the keyboard is open, so nothing jumps under the person.
function noticeSync(sync) {
  const before = S.sync || {};
  S.sync = sync;
  const grew = Object.entries(sync).some(([id, now]) => (now.found || 0) > ((before[id] || {}).found || 0));
  const finished = Object.keys(before).some((id) => !(id in sync));
  if (S.tab === "settings" && (grew || finished) && S.data) renderSettings();
  if (!(grew || finished) || !S.data) return;
  const now = Date.now();
  if (!finished && now - (S.syncReloaded || 0) < 20000) return;
  if (S.detail || !$("keyboard").hidden) return;
  S.syncReloaded = now;
  loadOverview().catch(() => {});
}

async function poll() {
  clearTimeout(S.pollTimer);
  try {
    applyStatus(await api("/api/store/status"));
  } catch (error) {
    /* the next poll tries again */
  }
  const lively = S.busy || S.embed || Object.values(S.jobs).some((j) => ACTIVE.includes(j.phase));
  S.pollTimer = setTimeout(poll, lively ? 1000 : 4000);
}

async function loadCovers() {
  if (S.coversRunning || !S.data) return;
  S.coversRunning = true;
  try {
    for (let round = 0; round < 40; round += 1) {
      const wanted = filtered().concat(S.games).filter((g) => !(g.id in S.covers));
      const ids = [...new Set(wanted.map((g) => g.id))].slice(0, 40);
      if (!ids.length) break;
      const result = await api("/api/store/covers", { ids });
      const fresh = Object.keys(result.covers || {}).filter((id) => !(id in S.covers));
      Object.assign(S.covers, result.covers || {});
      for (const id of fresh) setTileCover(id);
      if (!fresh.length) break;
    }
  } catch (error) {
    /* covers are a nicety; placeholders stay */
  } finally {
    S.coversRunning = false;
  }
}

// ------------------------------------------------------------------ discover

// Newest first: by release date, then the catalogue's own order (a site
// lists its newest post first). Games with no date go after, by name.
function byNewest(a, b) {
  const da = a.date || "", db = b.date || "";
  if (da && db && da !== db) return da < db ? 1 : -1;
  if (da && !db) return -1;
  if (!da && db) return 1;
  if (da) return (a.seq || 0) - (b.seq || 0);
  return a.name.localeCompare(b.name);
}

function sortMode() {
  return (S.data && S.data.settings && S.data.settings.sort) || "newest";
}

function filtered() {
  const query = S.query.trim().toLowerCase();
  const found = S.games.filter((game) => {
    if (S.catalogue !== "all" && game.catalogue !== S.catalogue) return false;
    if (S.tag !== "All" && !(game.tags || []).includes(S.tag)) return false;
    if (!query) return true;
    const text = [game.name, game.blurb, game.catalogue_name, ...(game.tags || [])].join(" ").toLowerCase();
    return query.split(/\s+/).every((word) => text.includes(word));
  });
  return sortMode() === "name" ? found : found.sort(byNewest);
}

function renderChips() {
  const box = $("chips");
  const focusKey = keyOf(document.activeElement);
  box.innerHTML = "";
  const sorter = make("button", "chip chip--sort nav", sortMode() === "name" ? "A to Z" : "Newest first");
  sorter.type = "button";
  sorter.dataset.key = "sort";
  sorter.addEventListener("click", () => {
    const next = sortMode() === "name" ? "newest" : "name";
    S.data.settings.sort = next;
    sorter.textContent = next === "name" ? "A to Z" : "Newest first";
    renderGrid();
    saveSettings({ sort: next });
  });
  box.append(sorter, make("span", "chips__rule"));
  const catalogues = (S.data.catalogues || []).filter((c) => c.enabled && c.count);
  if (catalogues.length > 1) {
    const all = { id: "all", name: "Every catalogue" };
    for (const entry of [all, ...catalogues]) {
      const chip = make("button", "chip chip--catalogue nav", entry.name);
      chip.type = "button";
      chip.dataset.key = `cat:${entry.id}`;
      chip.classList.toggle("is-on", S.catalogue === entry.id);
      chip.addEventListener("click", () => {
        S.catalogue = entry.id;
        S.tag = "All";
        renderChips();
        renderGrid();
      });
      box.append(chip);
    }
    box.append(make("span", "chips__rule"));
  }
  const pool = S.games.filter((g) => S.catalogue === "all" || g.catalogue === S.catalogue);
  const tags = [...new Set(pool.flatMap((g) => g.tags || []))].sort();
  if (S.tag !== "All" && !tags.includes(S.tag)) S.tag = "All";
  for (const tag of ["All", ...tags]) {
    const chip = make("button", "chip nav", tag === "All" ? "All games" : tag);
    chip.type = "button";
    chip.dataset.key = `tag:${tag}`;
    chip.classList.toggle("is-on", S.tag === tag);
    chip.addEventListener("click", () => {
      S.tag = tag;
      for (const other of box.querySelectorAll(".chip:not(.chip--catalogue)")) {
        other.classList.toggle("is-on", other === chip);
      }
      renderGrid();
    });
    box.append(chip);
  }
  restoreFocus(box, focusKey);
}

function renderNotices() {
  const box = $("notices");
  const focusKey = keyOf(document.activeElement);
  box.innerHTML = "";
  if (!S.data.library_ready) {
    const notice = make("div", "notice");
    const text = make("p", "notice__text");
    text.innerHTML = "<strong>Choose where games go.</strong> Pick a drive and a Games folder is made on it.";
    const actions = make("div", "notice__actions");
    for (const choice of S.data.library_choices || []) {
      actions.append(button(`${choice.path} · ${human(choice.free)} free`, "", () => chooseLibrary(choice.path),
                            `lib:${choice.path}`));
    }
    if (!(S.data.library_choices || []).length) {
      text.innerHTML += " Add a library folder in the main app, under Installs.";
    }
    notice.append(text, actions);
    box.append(notice);
  }
  for (const entry of S.data.catalogues || []) {
    if (!entry.error) continue;
    const notice = make("div", "notice");
    const text = make("p", "notice__text");
    text.append(make("strong", "", `${entry.name}: `), document.createTextNode(entry.error));
    notice.append(text);
    box.append(notice);
  }
  if (!S.data.artwork_key) {
    const notice = make("div", "notice notice--info");
    notice.append(make("p", "notice__text",
      "Cover art comes from SteamGridDB. Add a free API key in the main app's Settings to see it here."));
    box.append(notice);
  }
  restoreFocus(box, focusKey);
}

// Tiles are drawn a screenful at a time: a release website can put
// thousands of games on the shelf, and building every tile up front made
// opening the store and every keystroke in search slow.
const GRID_BATCH = 120;

function renderGrid() {
  const grid = $("grid");
  const focusKey = keyOf(document.activeElement);
  const games = filtered();
  grid.innerHTML = "";
  S.gridGames = games;
  S.gridShown = 0;
  // Keep enough drawn to put focus back where it was.
  const keep = focusKey && focusKey.startsWith("tile:")
    ? games.findIndex((g) => `tile:${g.id}` === focusKey) + 1 : 0;
  moreTiles(Math.max(GRID_BATCH, keep));
  $("gridEmpty").hidden = games.length > 0;
  $("searchText").textContent = S.query || "Search games";
  $("searchBox").classList.toggle("has-query", !!S.query);
  if (!restoreFocus(grid, focusKey) && grid.contains(document.activeElement) === false &&
      document.activeElement && document.activeElement.closest && document.activeElement.closest("#grid")) {
    focusFirst();
  }
}

function moreTiles(count = GRID_BATCH) {
  const grid = $("grid");
  const games = S.gridGames || [];
  const old = grid.querySelector(".grid__more");
  if (old) old.remove();
  const end = Math.min(games.length, S.gridShown + count);
  for (const game of games.slice(S.gridShown, end)) {
    const tile = make("button", "tile nav");
    tile.type = "button";
    tile.dataset.id = game.id;
    tile.dataset.key = `tile:${game.id}`;
    const art = make("div", "tile__art");
    art.append(placeholder(game));
    if (S.covers[game.id]) art.append(coverImage(S.covers[game.id]));
    const badge = make("span", "badge");
    badge.hidden = true;
    const bar = make("div", "tile__bar");
    bar.append(make("span"));
    bar.hidden = true;
    art.append(badge, bar);
    tile.append(art, make("p", "tile__name", game.name));
    tile.addEventListener("click", () => openDetail(game.id));
    // Reaching the last drawn row with a controller draws the next batch.
    tile.addEventListener("focus", () => {
      if (S.gridShown < (S.gridGames || []).length &&
          grid.children.length - Array.prototype.indexOf.call(grid.children, tile) < 16) moreTiles();
    });
    grid.append(tile);
    updateTile(tile, game);
  }
  S.gridShown = end;
  if (end < games.length) {
    const sentinel = make("div", "grid__more");
    grid.append(sentinel);
    if ("IntersectionObserver" in window) {
      if (S.gridObserver) S.gridObserver.disconnect();
      S.gridObserver = new IntersectionObserver((seen) => {
        if (seen.some((e) => e.isIntersecting)) moreTiles();
      }, { rootMargin: "800px" });
      S.gridObserver.observe(sentinel);
    }
  }
}

function updateTile(tile, game) {
  const state = gameState(game);
  const badge = tile.querySelector(".badge");
  const bar = tile.querySelector(".tile__bar");
  badge.hidden = !state.label;
  badge.textContent = state.label;
  badge.className = `badge badge--${state.kind}`;
  bar.hidden = state.kind !== "busy";
  if (state.kind === "busy") {
    const known = state.progress >= 0;
    bar.classList.toggle("is-unknown", !known);
    bar.firstChild.style.width = known ? `${Math.round(state.progress * 100)}%` : "";
  }
}

function updateTiles() {
  for (const tile of $("grid").children) {
    const game = S.byId.get(tile.dataset.id);
    if (game) updateTile(tile, game);
  }
}

function setTileCover(id) {
  const url = S.covers[id];
  if (!url) return;
  const tile = $("grid").querySelector(`.tile[data-id="${id}"]`);
  if (tile && !tile.querySelector("img")) tile.querySelector(".tile__art").insertBefore(
    coverImage(url), tile.querySelector(".badge"));
  if (S.detail === id && !$("detailCover").querySelector("img")) $("detailCover").append(coverImage(url));
}

// ------------------------------------------------------------------ a game's page

async function openDetail(id) {
  const game = S.byId.get(id);
  if (!game) return;
  S.lastFocus = document.activeElement;
  S.detail = id;
  S.detailSignature = "";
  $("detailTitle").textContent = game.name;
  const meta = $("detailMeta");
  meta.innerHTML = "";
  for (const part of [...(game.tags || []), game.version, game.extras, game.licence, game.company,
                     game.catalogue_name]) {
    if (part) meta.append(make("span", "", part));
  }
  $("detailBlurb").textContent = game.blurb || (S.detailArt[id] || {}).blurb || "";
  const warning = [game.needs_data, game.note].filter(Boolean).join(" ");
  $("detailWarn").hidden = !warning;
  $("detailWarn").textContent = warning;
  const cover = $("detailCover");
  cover.innerHTML = "";
  cover.append(placeholder(game));
  if (S.covers[id]) cover.append(coverImage(S.covers[id]));
  showArt(id);
  $("detail").hidden = false;
  renderDetailState();
  focusFirst();
  updateHints();

  if (!S.detailArt[id]) {
    try {
      S.detailArt[id] = await api("/api/store/art", { id });
      if (S.detail === id) {
        showArt(id);
        if (S.detailArt[id].blurb) $("detailBlurb").textContent = S.detailArt[id].blurb;
      }
    } catch (error) {
      S.detailArt[id] = {};
    }
  }
}

function showArt(id) {
  const art = S.detailArt[id] || {};
  const hero = $("detailHero");
  const backdrop = art.hero || art.cover || S.covers[id] || "";
  hero.style.backgroundImage = backdrop ? `url("${backdrop.replace(/"/g, "%22")}")` : "";
  hero.classList.toggle("is-blur", !art.hero);
  const logo = $("detailLogo");
  if (art.logo) {
    logo.onerror = () => { logo.hidden = true; };
    logo.src = art.logo;
    logo.hidden = false;
  } else {
    logo.hidden = true;
  }
  if (art.cover && !$("detailCover").querySelector("img")) $("detailCover").append(coverImage(art.cover));
}

function closeDetail() {
  $("detail").hidden = true;
  S.detail = "";
  const back = S.lastFocus && document.contains(S.lastFocus) ? S.lastFocus
    : $("grid").querySelector(".tile");
  if (back) focusOn(back);
  updateHints();
}

function renderDetailState() {
  if (!S.detail) return;
  const game = S.byId.get(S.detail);
  if (!game) return;
  const state = gameState(game);
  const progress = $("detailProgress");
  progress.hidden = state.kind !== "busy";
  if (state.kind === "busy") {
    const known = state.progress >= 0;
    progress.classList.toggle("is-unknown", !known);
    progress.firstChild.style.width = known ? `${Math.round(state.progress * 100)}%` : "";
  }
  const status = $("detailStatus");
  status.textContent = state.message || (state.kind !== "available" ? ""
    : game.download_size && game.size ? `${human(game.download_size)} to download, about ${human(game.size)} installed.`
    : game.size ? `About ${human(game.size)}.` : "");
  status.className = "detail__status" + (
    state.kind === "installed" && !state.warn ? " is-good"
    : state.kind === "pending" ? " is-pending"
    : ["failed", "waiting"].includes(state.kind) || state.warn ? " is-warn" : "");

  const job = state.job || {};
  const signature = `${state.kind}|${job.action || ""}|${S.steamRunning}`;
  if (signature === S.detailSignature) return;
  S.detailSignature = signature;
  const actions = $("detailActions");
  const hadFocus = actions.contains(document.activeElement) || document.activeElement === document.body;
  actions.innerHTML = "";
  const id = game.id;
  if (state.kind === "available") {
    actions.append(button("Install", "btn--primary btn--big", () => install(id), "d:install"));
    if (usableFolders().length > 1) {
      actions.append(button("Install to…", "btn--big btn--quiet",
                            () => pickDrive(game, (path) => install(id, path)), "d:installto"));
    }
  } else if (state.kind === "busy") {
    actions.append(button("Cancel", "btn--big", () => cancel(id), "d:cancel"));
  } else if (state.kind === "waiting") {
    if (job.action === "folder") {
      actions.append(button("Choose where games go", "btn--primary btn--big", () => {
        closeDetail();
        setTab("settings");
      }, "d:folder"));
    } else if (job.action === "sevenzip") {
      actions.append(button("Get 7-Zip", "btn--primary btn--big", async () => {
        try {
          await api("/api/tools/7zip", {});
          await api("/api/store/retry", { id });
        } catch (error) { toast(error.message, "bad"); }
      }, "d:7z"));
    } else {
      actions.append(button("Try again", "btn--primary btn--big", () => retry(id), "d:retry"));
    }
    actions.append(button("Cancel", "btn--big", () => cancel(id), "d:cancel"));
  } else if (state.kind === "failed") {
    actions.append(button("Try again", "btn--primary btn--big", () => install(id), "d:retry"));
  } else if (state.kind === "pending") {
    actions.append(button("Restart Steam now", "btn--primary btn--big", () => restartFlow(true), "d:restart"));
  } else if (state.kind === "installed" && state.record && state.record.steam === "added") {
    actions.append(button("Play", "btn--primary btn--big", () => play(id), "d:play"));
  }
  if (state.record && ["installed", "pending"].includes(state.kind)) {
    actions.append(button("Uninstall", "btn--big btn--quiet", () => confirmUninstall(id), "d:uninstall"));
  }
  if (hadFocus && !$("detail").hidden) focusFirst();
}

// ------------------------------------------------------------------ actions

async function install(id, target = null) {
  const game = S.byId.get(id) || { id, name: "this game" };
  if (target === null && S.data && S.data.library_ready && libraryState().ask && usableFolders().length > 1) {
    pickDrive(game, (path) => install(id, path));
    return;
  }
  if (S.data && !S.data.library_ready) {
    const choices = S.data.library_choices || [];
    if (!choices.length) {
      toast("Add a library folder in the main app first, under Installs.", "bad");
      return;
    }
    ask("Where should games go?", "Pick a drive. A Games folder is made on it, and games install there.",
      choices.map((choice) => ({
        label: `${choice.path} · ${human(choice.free)} free`,
        primary: false,
        run: async () => { await chooseLibrary(choice.path); install(id); },
      })).concat([{ label: "Not now", run: () => {} }]));
    return;
  }
  try {
    const { job } = await api("/api/store/install", { id, target: target || "" });
    S.jobs[id] = job;
    S.phases[id] = job.phase;
    applyStatus({ ...currentStatus(), jobs: S.jobs }, true);
    toast(`Getting ${job.name}`);
    poll();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function confirmUninstall(id) {
  const record = S.installed[id];
  if (!record) return;
  const steamLine = record.steam === "added"
    ? (S.steamRunning ? " It leaves your Steam library the next time Steam restarts." : " It leaves your Steam library too.")
    : "";
  ask(`Uninstall ${record.name}?`,
    `The game is removed from this PC.${steamLine} Saved games kept in your user folders are left alone.`, [
      { label: "Uninstall", run: () => uninstall(id) },
      { label: "Keep it", primary: true, run: () => {} },
    ]);
}

async function uninstall(id) {
  try {
    const { job } = await api("/api/store/uninstall", { id });
    S.jobs[id] = job;
    S.phases[id] = job.phase;
    applyStatus({ ...currentStatus(), jobs: S.jobs }, true);
    toast(`Uninstalling ${job.name}`);
    poll();
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function cancel(id) {
  try {
    applyStatus(await api("/api/store/cancel", { id }), true);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function retry(id) {
  try {
    await api("/api/store/retry", { id });
    poll();
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function play(id) {
  try {
    const result = await api("/api/store/play", { id });
    toast(result.message);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function chooseLibrary(path) {
  try {
    applyOverview(await api("/api/store/library", { path }));
    toast(`Games install into ${path}`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

function currentStatus() {
  return { jobs: S.jobs, installed: S.installed, pending: S.pending, pending_remove: S.pendingRemove, busy: S.busy,
           embed: S.embed, steam_running: S.steamRunning };
}

// ------------------------------------------------------------------ Steam restart and leaving

function restartText() {
  const where = S.data.settings.reopen === "library" ? "" : " in Big Picture";
  return `Steam closes, ${pendingSentence()}, and Steam opens again${where}.`;
}

function restartFlow(relaunch) {
  if (!pendingCount()) {
    toast("Nothing is waiting for Steam.");
    return;
  }
  if (S.busy) {
    ask("Installs are still running", "Restarting now would stop them. Steam can restart by itself once they finish.", [
      { label: "Restart when they finish", primary: true, run: () => restartSteam(relaunch, true) },
      { label: "Not now", run: () => {} },
    ]);
    return;
  }
  const back = relaunch && S.data.store_window ? " The store comes back afterwards." : "";
  ask("Restart Steam now?", restartText() + back, [
    { label: "Restart Steam", primary: true, run: () => restartSteam(relaunch, false) },
    { label: "Not now", run: () => {} },
  ]);
}

async function restartSteam(relaunch, whenIdle) {
  try {
    if (whenIdle) {
      await api("/api/store/steam", { relaunch, when_idle: true });
      toast("Steam restarts once the installs finish.");
      return;
    }
    $("coverUpText").textContent = "Restarting Steam…";
    $("coverUp").hidden = false;
    const result = await api("/api/store/steam", { relaunch });
    if (!result.leaving) {
      setTimeout(() => { $("coverUp").hidden = true; toast(result.message); poll(); }, 2500);
    }
  } catch (error) {
    $("coverUp").hidden = true;
    toast(error.message, "bad");
  }
}

function leave() {
  if (S.busy) {
    ask("Installs are still running", "Leaving stops them. Downloads pick up where they stopped next time.", [
      { label: "Stay in the store", primary: true, run: () => {} },
      { label: "Leave anyway", run: () => leaveSteamStep() },
    ]);
    return;
  }
  leaveSteamStep();
}

function leaveSteamStep() {
  if (pendingCount() && S.steamRunning) {
    ask("Steam needs a restart",
      `Steam only notices changes to its library when it starts. ${restartText()}`, [
        { label: "Restart Steam and leave", primary: true, run: () => restartSteam(false, false).then(quit) },
        { label: "Leave without restarting", run: quit },
        { label: "Stay in the store", run: () => {} },
      ]);
    return;
  }
  // One stray press of B should never throw someone out of the store.
  ask("Leave the store?", S.data && S.data.streaming
    ? "The store closes and this Moonlight stream ends. Start your Steam app in Moonlight next."
    : S.data && S.data.store_window ? "The store closes. Steam stays as it is."
    : "This goes back to the game manager.", [
    { label: "Leave", primary: true, run: quit },
    { label: "Stay in the store", run: () => {} },
  ]);
}

async function quit() {
  if (!S.data || !S.data.store_window) {
    window.location.href = "/";
    return;
  }
  $("coverUpText").textContent = "See you later";
  $("coverUp").hidden = false;
  try { await api("/api/quit", {}); } catch (error) { /* already gone */ }
}

// ------------------------------------------------------------------ library and downloads

function rowThumb(game) {
  const thumb = make("div", "row__thumb");
  thumb.append(placeholder(game));
  if (S.covers[game.id]) thumb.append(coverImage(S.covers[game.id]));
  return thumb;
}

function gameFor(id, name) {
  return S.byId.get(id) || { id, name: name || "Game", tags: [], installable: false };
}

function renderLibrary() {
  const box = $("libraryRows");
  const focusKey = keyOf(document.activeElement);
  box.innerHTML = "";
  const entries = Object.entries(S.installed).sort((a, b) => (b[1].at || 0) - (a[1].at || 0));
  for (const [id, record] of entries) {
    const game = gameFor(id, record.name);
    const state = gameState(game);
    const row = make("div", "row");
    const body = make("div");
    body.append(make("p", "row__title", record.name));
    const sub = make("p", "row__sub", state.message);
    if (state.kind === "pending") sub.classList.add("row__sub--pending");
    else if (state.warn) sub.classList.add("row__sub--warn");
    body.append(sub);
    if (record.dir) body.append(make("p", "row__sub", record.dir));
    const actions = make("div", "row__actions");
    if (record.steam === "added") actions.append(button("Play", "btn--primary", () => play(id), `lib:play:${id}`));
    if (S.byId.has(id)) actions.append(button("Details", "", () => openDetail(id), `lib:info:${id}`));
    if (state.kind === "busy") {
      body.querySelector(".row__sub").textContent = state.label;
    } else {
      actions.append(button("Uninstall", "btn--quiet", () => confirmUninstall(id), `lib:remove:${id}`));
    }
    row.append(rowThumb(game), body, actions);
    box.append(row);
  }
  $("libraryEmpty").hidden = entries.length > 0;
  const waiting = pendingCount();
  $("libraryBanner").hidden = !waiting;
  $("libraryBannerText").textContent = waiting ? `When Steam restarts, ${pendingSentence()}.` : "";
  restoreFocus($("view-library"), focusKey);
}

function renderDownloads() {
  const box = $("downloadRows");
  const focusKey = keyOf(document.activeElement);
  box.innerHTML = "";
  const jobs = Object.values(S.jobs)
    .filter((job) => job.phase !== "done" || Date.now() / 1000 - job.finished < 600)
    .sort((a, b) => (ACTIVE.includes(b.phase) - ACTIVE.includes(a.phase)) || (b.started - a.started));
  for (const job of jobs) {
    const game = gameFor(job.id, job.name);
    const state = gameState(game);
    const row = make("div", "row");
    const body = make("div");
    body.append(make("p", "row__title", job.name));
    const sub = make("p", "row__sub", job.message);
    if (job.phase === "failed" || job.phase === "waiting") sub.classList.add("row__sub--warn");
    if (job.phase === "done") sub.classList.add("row__sub--good");
    body.append(sub);
    if (state.kind === "busy") {
      const bar = make("div", "progress");
      bar.append(make("span"));
      const known = job.progress >= 0;
      bar.classList.toggle("is-unknown", !known);
      if (known) bar.firstChild.style.width = `${Math.round(job.progress * 100)}%`;
      body.append(bar);
    }
    const actions = make("div", "row__actions");
    if (S.embed && S.embed.task === job.task && S.installerHidden === S.embed.id) {
      actions.append(button("Show installer", "btn--primary", () => {
        S.installerHidden = "";
        renderInstaller(S.embed);
      }, `dl:show:${job.id}`));
    }
    if (ACTIVE.includes(job.phase)) actions.append(button("Cancel", "", () => cancel(job.id), `dl:cancel:${job.id}`));
    if (job.phase === "failed" || job.phase === "cancelled") {
      actions.append(button("Try again", "", () => install(job.id), `dl:retry:${job.id}`));
    }
    if (job.phase === "waiting") actions.append(button("Try again", "", () => retry(job.id), `dl:retry:${job.id}`));
    if (S.byId.has(job.id)) actions.append(button("Details", "", () => openDetail(job.id), `dl:info:${job.id}`));
    row.append(rowThumb(game), body, actions);
    box.append(row);
  }
  $("downloadEmpty").hidden = jobs.length > 0;
  restoreFocus(box, focusKey);
}

function renderDownloadCount() {
  const active = Object.values(S.jobs).filter((job) => ACTIVE.includes(job.phase)).length;
  const badge = $("downloadCount");
  badge.hidden = !active;
  badge.textContent = active;
}

function pendingCount() { return S.pending.length + S.pendingRemove.length; }

function pendingSentence() {
  const parts = [];
  if (S.pending.length) parts.push(`${S.pending.join(", ")} ${S.pending.length === 1 ? "goes" : "go"} into Steam`);
  if (S.pendingRemove.length) parts.push(`${S.pendingRemove.join(", ")} ${S.pendingRemove.length === 1 ? "leaves" : "leave"} it`);
  return parts.join("; ");
}

function renderPending() {
  const chip = $("pendingChip");
  const count = pendingCount();
  chip.hidden = !count;
  chip.textContent = count ? `Steam restart needed (${count})` : "";
}

// ------------------------------------------------------------------ settings

function renderSettings() {
  const settings = S.data.settings;
  for (const [id, value] of [["optRestart", settings.restart], ["optReopen", settings.reopen],
                             ["optGlyphs", settings.glyphs || "playstation"],
                             ["optSiteMethod", settings.site_method || "auto"],
                             ["optParallel", String(settings.parallel || 2)],
                             ["optTorrentEngine", (settings.torrent || {}).engine || "builtin"]]) {
    for (const seg of $(id).querySelectorAll(".seg")) seg.classList.toggle("is-on", seg.dataset.value === value);
  }
  $("optKeep").setAttribute("aria-checked", settings.keep_downloads ? "true" : "false");
  $("optOptional").setAttribute("aria-checked", settings.optional_files ? "true" : "false");
  $("optMute").setAttribute("aria-checked", settings.mute_installers !== false ? "true" : "false");
  const every = Number(settings.site_pages) === 0;
  $("optAllPages").setAttribute("aria-checked", every ? "true" : "false");
  $("sitePagesLine").hidden = every;
  $("sitePagesValue").textContent = every ? "All" : String(settings.site_pages || 10);
  $("optSeed").setAttribute("aria-checked", settings.keep_seeding ? "true" : "false");
  const torrent = settings.torrent || {};
  const builtin = (torrent.engine || "builtin") === "builtin" && torrent.builtin_available;
  $("torrentLogin").hidden = builtin;
  $("torrentLook").hidden = builtin;
  $("seedLine").hidden = builtin;
  $("torrentTitle").textContent = builtin ? "Built-in downloader" : (torrent.label || "Your torrent app");
  $("torrentState").textContent = builtin
    ? (torrent.builtin_ready ? "Ready. Nothing else needs to be open. It downloads, then stops: no sharing afterwards."
      : "Fetched the first time a game needs it (2.5 MB). Nothing else needs to be open, and nothing is shared afterwards.")
    : (torrent.engine === "builtin" && !torrent.builtin_available)
    ? "The built-in downloader needs aria2 on this system (install it with your package manager). Until then your torrent app is used."
    : torrent.label
    ? `${torrent.label} at ${torrent.url.replace(/^https?:\/\//, "")}${torrent.username ? `, signed in as ${torrent.username}` : ""}. Games download through it, and it is tidied up after each install.`
    : "None found yet. Start Transmission or qBittorrent with remote access on, then press Look for it.";

  $("sunshineCommand").textContent = S.data.sunshine_command || "";
  $("streamState").textContent = S.data.streaming
    ? "This store is running as its own Sunshine app. Leaving it ends the stream; Steam restarts quietly behind it when new games need it."
    : "Give the store its own app in Sunshine, next to Steam, to open it straight from Moonlight.";

  const waitingSelf = S.pending.includes("Game Store");
  $("addSelf").textContent = S.data.store_shortcut ? "Add again" : waitingSelf ? "Waiting for restart" : "Add to Steam";
  $("addSelf").disabled = waitingSelf;
  $("selfState").textContent = S.data.store_shortcut
    ? "It is in your Steam library, so it opens from Big Picture."
    : waitingSelf ? "It appears in Steam after Steam restarts."
    : "Adds a “Game Store” tile, so it can be opened from Big Picture.";

  const pick = $("libraryPick");
  const focusKey = keyOf(document.activeElement);
  pick.innerHTML = "";
  if (S.data.library_ready) {
    renderDrives(pick);
  } else {
    pick.append(make("p", "group__lede", "Choose where games go. A Games folder is made on the drive you pick."));
    const list = make("div", "choices");
    for (const choice of S.data.library_choices || []) {
      const item = make("button", "choice nav");
      item.type = "button";
      item.dataset.key = `set:lib:${choice.path}`;
      item.append(make("span", "", choice.path), make("small", "", `${human(choice.free)} free of ${human(choice.total)}`));
      item.addEventListener("click", () => chooseLibrary(choice.path));
      list.append(item);
    }
    pick.append(list);
  }

  const rows = $("catalogueRows");
  rows.innerHTML = "";
  for (const entry of S.data.catalogues || []) {
    const row = make("div", "row row--catalogue");
    const body = make("div");
    body.append(make("p", "row__title", entry.name));
    const detail = entry.builtin ? (entry.enabled ? `${entry.count} free and open-source games, built in`
                                                  : "Built in · switched off, so its games are hidden")
      : !entry.enabled ? `Switched off · ${entry.source}`
      : entry.error ? `${entry.error} · ${entry.source}`
      : entry.kind === "site" ? `${plural(entry.count, "game", "games")} · ${siteProgress(entry)} · ${entry.source}`
      : `${plural(entry.count, "game", "games")} · ${entry.source}`;
    const sub = make("p", "row__sub", detail);
    if (entry.error) sub.classList.add("row__sub--warn");
    body.append(sub);
    const actions = make("div", "row__actions");
    actions.append(button(entry.enabled ? "Switch off" : "Switch on", "", () => changeCatalogue(entry.id, "toggle"),
                          `catrow:toggle:${entry.id}`));
    if (!entry.builtin) {
      actions.append(button("Remove", "btn--danger", () => changeCatalogue(entry.id, "remove"),
                            `catrow:remove:${entry.id}`));
    }
    row.append(body, actions);
    rows.append(row);
  }
  restoreFocus($("view-settings"), focusKey);
}

// ------------------------------------------------------------------ where games go

function libraryState() {
  return (S.data && S.data.library) || { folders: [], choices: [], ask: false };
}

function usableFolders() {
  return libraryState().folders.filter((f) => f.enabled && f.connected);
}

function driveText(folder) {
  const kind = { ssd: "SSD", hdd: "Hard disk" }[folder.kind] || "Drive";
  const parts = [kind];
  if (!folder.connected) parts.push("not connected");
  else parts.push(`${human(folder.free)} free of ${human(folder.total)}`);
  if (!folder.enabled) parts.push("switched off");
  else if (folder.priority === 1) parts.push("first choice");
  return parts.join(" · ");
}

function renderDrives(box) {
  const lib = libraryState();
  box.append(make("p", "group__lede",
    "Games go to the first folder below with room for them (and an SSD, for games that need one). " +
    "Put a drive first to fill it before the others."));
  for (const folder of lib.folders) {
    const row = make("div", "drive" + (folder.enabled ? "" : " is-off"));
    const body = make("div");
    body.append(make("p", "line__title", folder.path), make("p", "line__sub", driveText(folder)));
    const actions = make("div", "drive__actions");
    if (folder.priority !== 1 || !folder.enabled) {
      actions.append(button("Use first", "", () => changeDrive("first", folder.path), `drive:first:${folder.path}`));
    }
    actions.append(button(folder.enabled ? "Switch off" : "Switch on", "",
                          () => changeDrive("toggle", folder.path), `drive:toggle:${folder.path}`));
    actions.append(button("Remove", "btn--danger", () => confirmRemoveDrive(folder.path),
                          `drive:remove:${folder.path}`));
    row.append(body, actions);
    box.append(row);
  }
  if (lib.choices.length) {
    box.append(make("p", "label", "Add another drive"));
    const list = make("div", "choices");
    for (const choice of lib.choices) {
      const item = make("button", "choice nav");
      item.type = "button";
      item.dataset.key = `drive:add:${choice.path}`;
      item.append(make("span", "", choice.path), make("small", "", `${human(choice.free)} free of ${human(choice.total)}`));
      item.addEventListener("click", () => changeDrive("add", choice.path));
      list.append(item);
    }
    box.append(list);
  }
  const line = make("div", "line");
  const text = make("div");
  text.append(make("p", "line__title", "Ask which drive for each game"),
              make("p", "line__sub", "Off: games go where the order above says. A game's page also has Install to…"));
  const toggle = make("button", "toggle nav");
  toggle.type = "button";
  toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", lib.ask ? "true" : "false");
  toggle.dataset.key = "drive:ask";
  toggle.append(make("span"));
  toggle.addEventListener("click", async () => {
    if (await saveSettings({ ask_drive: !lib.ask })) {
      S.data.library.ask = !lib.ask;
      renderSettings();
    }
  });
  line.append(text, toggle);
  box.append(line);
}

async function changeDrive(action, path) {
  try {
    const result = await api("/api/store/library", { action, path, keep_view: true });
    S.data.library = result.library;
    S.data.library_ready = result.library.folders.some((f) => f.enabled);
    renderSettings();
    if (action === "add") toast(`Games can go to ${path} now`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

function confirmRemoveDrive(path) {
  ask("Take this folder out of the list?", `${path}: games already there stay where they are; ` +
      "nothing is deleted. New games just will not go there.", [
    { label: "Take it out", primary: true, run: () => changeDrive("remove", path) },
    { label: "Keep it", run: () => {} },
  ]);
}

function pickDrive(game, then) {
  const folders = usableFolders();
  const need = game.size || 0;
  ask(`Install ${game.name} to…`, need ? `It needs about ${human(need)}.` : "Pick a drive.",
    folders.map((folder, index) => ({
      label: `${folder.path} · ${human(folder.free)} free${need && folder.free < need ? " (too small)" : ""}`,
      primary: index === 0,
      run: () => then(folder.path),
    })).concat([{ label: "Choose for me", run: () => then("") },
                { label: "Not now", run: () => {} }]));
}

// ------------------------------------------------------------------ signing in to a torrent app

function torrentLogin() {
  const torrent = (S.data.settings && S.data.settings.torrent) || {};
  openKeyboard({
    label: "Torrent app username",
    value: torrent.username || "",
    onDone: (username) => {
      openKeyboard({
        label: "Torrent app password",
        value: "",
        secret: true,
        onDone: async (password) => {
          try {
            const found = await api("/api/store/torrent-login", { username, password });
            S.data.settings.torrent = found;
            S.data.settings.torrent.engine = "app";
            renderSettings();
            toast(found.message, "good");
          } catch (error) {
            toast(error.message, "bad");
          }
          focusOn($("torrentLogin"));
        },
      });
    },
  });
}

function siteProgress(entry) {
  const sync = { ...(entry.sync || {}), ...((S.sync || {})[entry.id] || {}) };
  const total = sync.total_pages || sync.of || 0;
  const of = total ? ` of ${total}` : "";
  if (sync.running || sync.phase) {
    return sync.phase === "new" ? "checking for new releases"
      : `reading page ${sync.page || 1}${of}`;
  }
  if (sync.complete) return `all ${plural(sync.pages_read || total, "page", "pages")} read`;
  return `${plural(sync.pages_read || 0, "page", "pages")}${of} read`;
}

async function saveSettings(patch) {
  try {
    const { settings } = await api("/api/store/settings", patch);
    S.data.settings = settings;
    renderSettings();
    return settings;
  } catch (error) {
    toast(error.message, "bad");
    return null;
  }
}

async function changeCatalogue(id, how) {
  if (id === "builtin") {
    if (await saveSettings({ builtin: !S.data.settings.builtin })) {
      await loadOverview();
      focusFirst();
    }
    return;
  }
  let list = S.data.settings.catalogues.map((c) => ({ ...c }));
  if (how === "remove") list = list.filter((c) => c.id !== id);
  else list = list.map((c) => (c.id === id ? { ...c, enabled: !c.enabled } : c));
  if (await saveSettings({ catalogues: list })) {
    await loadOverview();
    focusFirst();
  }
}

async function checkCatalogue() {
  const source = $("catalogueSource").value.trim();
  const box = $("catalogueResult");
  S.checked = null;
  $("catalogueAdd").hidden = true;
  box.hidden = false;
  box.classList.remove("is-error");
  $("catalogueResultText").textContent = "Reading it…";
  try {
    const found = await api("/api/store/catalogues/test", { source });
    S.checked = { source, ...found };
    const text = $("catalogueResultText");
    text.innerHTML = "";
    text.append(make("strong", "", found.name || "Untitled catalogue"),
      document.createTextNode(` · ${plural(found.count, "game", "games")}`
        + (found.kind === "site" ? ` on the front page, ${plural(found.pages || 1, "page", "pages")} in all` : "")
        + `, such as ${found.sample.join(", ")}`
        + (found.skipped ? `. ${found.skipped} entries could not be used.` : ".")));
    $("catalogueAdd").hidden = false;
    focusOn($("catalogueAdd"));
  } catch (error) {
    box.classList.add("is-error");
    $("catalogueResultText").textContent = error.message;
  }
}

async function addCatalogue() {
  if (!S.checked) return;
  const list = S.data.settings.catalogues.concat([{ source: S.checked.source, name: S.checked.name, enabled: true }]);
  if (await saveSettings({ catalogues: list })) {
    toast(`Added ${S.checked.name || "the catalogue"}`);
    S.checked = null;
    $("catalogueSource").value = "";
    $("catalogueResult").hidden = true;
    await loadOverview();
    focusOn($("catalogueSource"));
  }
}

// ------------------------------------------------------------------ questions

const DIALOG = { choices: [], returnTo: null };

function ask(title, text, choices) {
  DIALOG.returnTo = document.activeElement;
  DIALOG.choices = choices;
  $("dialogTitle").textContent = title;
  $("dialogText").textContent = text;
  const box = $("dialogActions");
  box.innerHTML = "";
  choices.forEach((choice, index) => {
    box.append(button(choice.label, choice.primary ? "btn--primary" : "", () => {
      closeDialog();
      choice.run();
    }, `dialog:${index}`));
  });
  $("dialog").hidden = false;
  focusFirst();
  updateHints();
  syncEmbed();
}

function closeDialog() {
  $("dialog").hidden = true;
  const back = DIALOG.returnTo;
  if (back && document.contains(back) && back.offsetParent !== null) focusOn(back);
  else focusFirst();
  updateHints();
  syncEmbed();
}

// ------------------------------------------------------------------ installer window

function renderInstaller(session) {
  S.embed = session;
  const sheet = $("installer");
  const wanted = !!(session && (session.kind === "classic" || session.has_window) &&
                    S.installerHidden !== session.id);
  if (wanted && sheet.hidden) {
    S.installerReturn = document.activeElement;
    sheet.hidden = false;
    focusFirst();
    updateHints();
  } else if (!wanted && !sheet.hidden) {
    sheet.hidden = true;
    if (S.installerReturn && document.contains(S.installerReturn)) focusOn(S.installerReturn);
    else focusFirst();
    updateHints();
  }
  if (session) {
    $("installerTitle").textContent = session.window || session.title || "Installer";
    $("installerSub").textContent = session.window ? session.title : "Answer its questions to carry on.";
    $("installerPop").textContent = session.popped ? "Bring back" : "Pop out";
    $("installerPop").hidden = !session.available;
    const notes = {
      starting: "Starting the installer…",
      floating: "It is open in a window of its own.",
    };
    $("installerNote").textContent = session.reason || notes[session.state] || "";
  }
  syncEmbed();
  // Once the installer's window is held and showing, say what is selected
  // in it, so the controller has something to start from.
  const remoteKey = installerRemote() ? session.id : "";
  if (remoteKey !== S.remoteKey) {
    S.remoteKey = remoteKey;
    updateHints();
    if (remoteKey) setTimeout(() => { if (installerRemote()) sendRemote("describe"); }, 300);
    else $("installerFocus").textContent = "";
  }
}

function syncEmbed() {
  const session = S.embed;
  const visible = !!(session && session.available && !session.popped && !$("installer").hidden &&
                     $("dialog").hidden && $("keyboard").hidden && !document.hidden);
  let rect = null;
  if (visible) {
    const box = $("installerStage").getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    rect = [box.left, box.top, box.width, box.height].map((v) => Math.round(v * scale));
  }
  const key = JSON.stringify([visible, rect]);
  if (key === S.embedKey) return;
  S.embedKey = key;
  api("/api/embed/place", { visible, rect }).catch(() => { S.embedKey = ""; });
}

// ------------------------------------------------------------------ on-screen keyboard

const KB_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl:", "zxcvbnm./-"];
const KB_SHIFT = { 1: "!", 2: "@", 3: "#", 4: "$", 5: "%", 6: "&", 7: "*", 8: "(", 9: ")", 0: "=",
                   ":": ";", ".": "?", "/": "\\", "-": "_" };
const KB = { value: "", shift: false, onInput: null, onDone: null, returnTo: null, built: false };

function buildKeyboard() {
  const box = $("kbKeys");
  box.innerHTML = "";
  KB_ROWS.forEach((row, r) => {
    const line = make("div", "kb__row");
    [...row].forEach((ch, c) => {
      const key = make("button", "key nav");
      key.type = "button";
      key.dataset.key = `kb:${r}:${c}`;
      key.dataset.char = ch;
      key.addEventListener("click", () => kbType(kbChar(ch)));
      line.append(key);
    });
    box.append(line);
  });
  const wide = make("div", "kb__row kb__row--wide");
  const actions = [
    ["Shift", "kb:shift", () => { KB.shift = !KB.shift; paintKeys(); }, ""],
    ["Space", "kb:space", () => kbType(" "), "y"],
    ["Delete", "kb:del", kbBackspace, "x"],
    ["Clear", "kb:clear", () => kbSet(""), ""],
    ["Done", "kb:done", kbDone, "menu"],
  ];
  for (const [label, keyName, run, button] of actions) {
    const key = make("button", `key key--action nav${keyName === "kb:done" ? " key--done" : ""}`);
    key.type = "button";
    key.dataset.key = keyName;
    if (button) key.append(glyph(button));
    key.append(document.createTextNode(label));
    key.addEventListener("click", run);
    wide.append(key);
  }
  box.append(wide);
  KB.built = true;
  paintKeys();
}

function kbChar(ch) {
  if (!KB.shift) return ch;
  return KB_SHIFT[ch] || ch.toUpperCase();
}

function paintKeys() {
  for (const key of $("kbKeys").querySelectorAll("[data-char]")) key.textContent = kbChar(key.dataset.char);
  const shift = $("kbKeys").querySelector('[data-key="kb:shift"]');
  if (shift) shift.classList.toggle("is-on", KB.shift);
}

function openKeyboard({ label, value, onInput, onDone, secret = false }) {
  if (!KB.built) buildKeyboard();
  KB.secret = secret;
  KB.returnTo = document.activeElement;
  KB.onInput = onInput;
  KB.onDone = onDone;
  KB.shift = false;
  paintKeys();
  $("kbLabel").textContent = label;
  kbSet(value || "", true);
  $("keyboard").hidden = false;
  focusOn($("kbKeys").querySelector('[data-key="kb:1:0"]'));
  updateHints();
  syncEmbed();
}

function kbSet(value, silent = false) {
  KB.value = value.slice(0, 2000);
  $("kbValue").textContent = KB.secret ? "•".repeat(KB.value.length) : KB.value;
  if (!silent && KB.onInput) KB.onInput(KB.value);
}

function kbType(text) {
  kbSet(KB.value + text);
  if (KB.shift) { KB.shift = false; paintKeys(); }
}

function kbBackspace() { kbSet(KB.value.slice(0, -1)); }

function kbDone() {
  $("keyboard").hidden = true;
  const done = KB.onDone;
  KB.onDone = null;
  updateHints();
  syncEmbed();
  if (done) done(KB.value);
  else if (KB.returnTo && document.contains(KB.returnTo)) focusOn(KB.returnTo);
}

function openSearch(firstChar = "") {
  if (S.tab !== "discover") setTab("discover");
  openKeyboard({
    label: "Search",
    value: S.query + firstChar,
    onInput: (value) => { S.query = value; renderGrid(); },
    onDone: () => {
      const first = $("grid").querySelector(".tile");
      focusOn(S.query && first ? first : $("searchBox"));
    },
  });
  if (firstChar) { S.query = KB.value; renderGrid(); }
}

function openKeyboardFor(input) {
  openKeyboard({
    label: input.placeholder || "Type",
    value: input.value,
    onInput: (value) => { input.value = value; },
    onDone: () => focusOn(input.id === "catalogueSource" ? $("catalogueCheck") : input),
  });
}

// ------------------------------------------------------------------ focus and spatial movement

function keyOf(node) {
  return node && node.dataset ? node.dataset.key || "" : "";
}

function restoreFocus(container, key) {
  if (!key) return false;
  const match = [...container.querySelectorAll(".nav")].find((n) => n.dataset.key === key);
  if (match && (document.activeElement === document.body || !document.contains(document.activeElement) ||
                container.contains(document.activeElement) || document.activeElement === match)) {
    focusOn(match, false);
    return true;
  }
  return false;
}

function scopeRoot() {
  for (const id of ["dialog", "keyboard", "installer", "detail"]) {
    if (!$(id).hidden) return $(id);
  }
  return $("app");
}

function visible(node) {
  if (node.disabled || node.hidden) return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && node.getClientRects().length > 0;
}

function focusables(root = scopeRoot()) {
  return [...root.querySelectorAll(".nav")].filter(visible);
}

function focusOn(node, scroll = true) {
  if (!node) return;
  node.focus({ preventScroll: true });
  if (scroll) ensureVisible(node);
  const view = node.closest(".view");
  if (view && node.dataset.key) S.memory[view.id] = node.dataset.key;
}

function focusFirst() {
  const root = scopeRoot();
  if (root === $("app")) {
    const view = $(`view-${S.tab}`);
    const remembered = S.memory[view.id];
    const items = focusables(view);
    const pick = items.find((n) => n.dataset.key && n.dataset.key === remembered)
      || (S.tab === "discover" ? view.querySelector(".tile") : null) || items[0];
    if (pick) { focusOn(pick); return; }
    focusOn(document.querySelector(".tab.is-active"));
    return;
  }
  const items = focusables(root);
  const primary = items.find((n) => n.classList.contains("btn--primary"));
  focusOn(primary || items[0]);
}

function ensureVisible(node) {
  const strip = node.closest(".chips");
  if (strip) {
    const s = strip.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    if (r.left < s.left + 12) strip.scrollLeft -= s.left + 12 - r.left;
    else if (r.right > s.right - 12) strip.scrollLeft += r.right - s.right + 12;
  }
  const view = node.closest(".view");
  if (!view) return;
  const v = view.getBoundingClientRect();
  const r = node.getBoundingClientRect();
  const toolbar = view.querySelector(".toolbar");
  const top = v.top + (toolbar && !toolbar.contains(node) ? toolbar.getBoundingClientRect().height : 0) + 24;
  if (toolbar && toolbar.contains(node)) return;
  if (r.top < top) view.scrollTop -= top - r.top;
  else if (r.bottom > v.bottom - 32) view.scrollTop += r.bottom - v.bottom + 32;
}

function move(direction) {
  const items = focusables();
  const current = document.activeElement;
  if (!items.includes(current)) { focusFirst(); return; }
  const a = current.getBoundingClientRect();
  const ax = a.left + a.width / 2;
  const ay = a.top + a.height / 2;
  const gapX = (b) => Math.max(0, Math.max(b.left - a.right, a.left - b.right))
    + Math.abs(b.left + b.width / 2 - ax) * 0.1;

  if (direction === "up" || direction === "down") {
    // Up and down go to the nearest row first, then to whatever in that
    // row lines up best. Weighing both at once sent a form's right-hand
    // button past its neighbour to a left-aligned control far below.
    const down = direction === "down";
    const candidates = [];
    for (const node of items) {
      if (node === current) continue;
      const b = node.getBoundingClientRect();
      const by = b.top + b.height / 2;
      if (down ? by <= ay + 2 : by >= ay - 2) continue;
      candidates.push({ node, b, edge: down ? b.top : -b.bottom });
    }
    if (!candidates.length) return;
    const nearest = candidates.reduce((x, y) => (y.edge < x.edge ? y : x));
    const tolerance = Math.max(12, nearest.b.height * 0.5);
    const row = candidates.filter((c) => c.edge <= nearest.edge + tolerance);
    row.sort((x, y) => gapX(x.b) - gapX(y.b));
    focusOn(row[0].node);
    return;
  }

  let best = null;
  let bestScore = Infinity;
  for (const node of items) {
    if (node === current) continue;
    const b = node.getBoundingClientRect();
    const bx = b.left + b.width / 2;
    const by = b.top + b.height / 2;
    let along;
    let across;
    if (direction === "right") {
      if (bx <= ax + 2 || b.left < a.left + 2) continue;
      along = Math.max(0, b.left - a.right);
      across = Math.max(0, Math.max(b.top - a.bottom, a.top - b.bottom));
      across += Math.abs(by - ay) * 0.1;
    } else {
      if (bx >= ax - 2 || b.right > a.right - 2) continue;
      along = Math.max(0, a.left - b.right);
      across = Math.max(0, Math.max(b.top - a.bottom, a.top - b.bottom));
      across += Math.abs(by - ay) * 0.1;
    }
    const score = along + across * 3;
    if (score < bestScore) { bestScore = score; best = node; }
  }
  if (best) focusOn(best);
}

// ------------------------------------------------------------------ tabs

function setTab(name) {
  if (!TABS.includes(name)) return;
  S.tab = name;
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("is-active", tab.dataset.tab === name);
  for (const id of TABS) $(`view-${id}`).hidden = id !== name;
  if (name === "library") renderLibrary();
  if (name === "downloads") renderDownloads();
  if (name === "settings") renderSettings();
  if (scopeRoot() === $("app")) focusFirst();
  updateHints();
}

function cycleTab(step) {
  const index = TABS.indexOf(S.tab);
  setTab(TABS[(index + step + TABS.length) % TABS.length]);
}

// ------------------------------------------------------------------ input

function back() {
  if (!$("dialog").hidden) { closeDialog(); return; }
  if (!$("keyboard").hidden) { kbDone(); return; }
  if (!$("installer").hidden) {
    if (S.embed) S.installerHidden = S.embed.id;
    renderInstaller(S.embed);
    toast("The installer keeps going. Downloads has a button to show it again.");
    return;
  }
  if (!$("detail").hidden) { closeDetail(); return; }
  if (S.tab !== "discover") { setTab("discover"); return; }
  if (S.query) { S.query = ""; renderGrid(); focusFirst(); return; }
  leave();
}

function activate(node) {
  if (!node || !node.classList || !node.classList.contains("nav") || !visible(node)) {
    focusFirst();
    return;
  }
  if (node.tagName === "INPUT") { openKeyboardFor(node); return; }
  node.click();
}

const STICK = { sup: "up", sdown: "down", sleft: "left", sright: "right" };

function act(name) {
  setInput("pad");
  if (installerRemote()) { remoteAct(name); return; }
  name = STICK[name] || name;
  const keyboard = !$("keyboard").hidden;
  switch (name) {
    case "up": case "down": case "left": case "right":
      move(name);
      break;
    case "a":
      activate(document.activeElement);
      break;
    case "b":
      back();
      break;
    case "x":
      if (keyboard) kbBackspace();
      else if (scopeRoot() === $("app")) openSearch();
      break;
    case "y":
      if (keyboard) kbType(" ");
      else if (scopeRoot() === $("app")) setTab("downloads");
      break;
    case "lb": case "rb":
      if (scopeRoot() === $("app")) cycleTab(name === "lb" ? -1 : 1);
      break;
    case "menu":
      if (keyboard) kbDone();
      else if (scopeRoot() === $("app")) setTab("settings");
      break;
    case "view":
      if (scopeRoot() === $("app")) leave();
      break;
    default:
      break;
  }
}

function setInput(kind) {
  if (document.body.dataset.input !== kind) {
    document.body.dataset.input = kind;
    updateHints();
  }
}

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.altKey || event.metaKey) return;
  const inInput = event.target && event.target.tagName === "INPUT";

  if (!$("keyboard").hidden) {
    // A real keyboard works on the on-screen one too.
    if (event.key.length === 1) { event.preventDefault(); kbType(event.key); return; }
    if (event.key === "Backspace") { event.preventDefault(); kbBackspace(); return; }
    if (event.key === "Escape") { event.preventDefault(); kbDone(); return; }
  }

  if (installerRemote()) {
    const keys = { Tab: event.shiftKey ? "prev" : "next", ArrowUp: "up", ArrowDown: "down",
                   ArrowLeft: "left", ArrowRight: "right", Enter: "enter", " ": "press" };
    if (keys[event.key]) { event.preventDefault(); setInput("pad"); sendRemote(keys[event.key]); return; }
    if (event.key === "Escape") { event.preventDefault(); back(); return; }
  }

  switch (event.key) {
    case "ArrowUp": event.preventDefault(); act("up"); return;
    case "ArrowDown": event.preventDefault(); act("down"); return;
    case "ArrowLeft": if (inInput) return; event.preventDefault(); act("left"); return;
    case "ArrowRight": if (inInput) return; event.preventDefault(); act("right"); return;
    case "Escape": event.preventDefault(); act("b"); return;
    case "Backspace": if (inInput) return; event.preventDefault(); act("b"); return;
    case "PageUp": case "[": if (inInput) return; event.preventDefault(); act("lb"); return;
    case "PageDown": case "]": if (inInput) return; event.preventDefault(); act("rb"); return;
    case "Enter":
      setInput("pad");
      if (inInput) {
        event.preventDefault();
        if (event.target.id === "catalogueSource") checkCatalogue();
      }
      return;          // buttons answer Enter themselves
    case " ":
      setInput("pad");
      return;
    default:
      break;
  }
  if (!inInput && event.key.length === 1 && /\S/.test(event.key) && scopeRoot() === $("app") &&
      S.tab === "discover") {
    event.preventDefault();
    setInput("pad");
    openSearch(event.key);
  }
});

let lastMouse = [0, 0];
document.addEventListener("mousemove", (event) => {
  const moved = Math.abs(event.screenX - lastMouse[0]) + Math.abs(event.screenY - lastMouse[1]);
  lastMouse = [event.screenX, event.screenY];
  if (moved > 6) setInput("mouse");
});

// Gamepads -------------------------------------------------------------------

const PAD = { previous: {}, repeatAt: {}, connected: false, detected: "xbox" };
const REPEATS = new Set(["up", "down", "left", "right", "sup", "sdown", "sleft", "sright", "x"]);
const PAD_BUTTONS = ["a", "b", "x", "y", "lb", "rb", "view", "menu",
                     "up", "down", "left", "right", "sup", "sdown", "sleft", "sright"];

// What a controller is, from the name the browser reports. Sony's USB
// vendor id is 054c: DualShock 4 pads are 05c4, 09cc and 0ba0, DualSense
// 0ce6 and the Edge 0df2. Sunshine's emulated DualShock 4 reports the
// same, so a PlayStation pad over Moonlight is recognised too.
function padFamily(id) {
  const text = (id || "").toLowerCase();
  if (/0ce6|0df2|dualsense/.test(text)) return "ps5";
  if (/054c|dualshock|playstation|wireless controller/.test(text)) return "ps4";
  return "xbox";
}

function readPads() {
  const state = {};
  let active = null;
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  for (const pad of pads) {
    const pressed = (index) => !!(pad.buttons[index] && pad.buttons[index].pressed);
    const x = pad.axes[0] || 0;
    const y = pad.axes[1] || 0;
    if (pad.buttons.some((b) => b && b.pressed) || Math.abs(x) > 0.55 || Math.abs(y) > 0.55) active = pad;
    state.a = state.a || pressed(0);
    state.b = state.b || pressed(1);
    state.x = state.x || pressed(2);
    state.y = state.y || pressed(3);
    state.lb = state.lb || pressed(4);
    state.rb = state.rb || pressed(5);
    state.view = state.view || pressed(8);
    state.menu = state.menu || pressed(9);
    state.up = state.up || pressed(12);
    state.down = state.down || pressed(13);
    state.left = state.left || pressed(14);
    state.right = state.right || pressed(15);
    // The left stick is kept apart from the d-pad: in the store it moves the
    // same way, but inside an installer it scrolls lists instead.
    state.sup = state.sup || y < -0.55;
    state.sdown = state.sdown || y > 0.55;
    state.sleft = state.sleft || x < -0.55;
    state.sright = state.sright || x > 0.55;
    state.scroll = Math.abs(pad.axes[3] || 0) > 0.25 ? pad.axes[3] : (state.scroll || 0);
  }
  return { state, count: pads.length, active };
}

function padLoop(now) {
  const { state, count, active } = readPads();
  if (active) {
    const family = padFamily(active.id);
    if (family !== PAD.detected) {
      PAD.detected = family;
      paintGlyphs();
    }
  }
  if (count) {
    for (const name of PAD_BUTTONS) {
      const down = !!state[name];
      const was = !!PAD.previous[name];
      if (down && !was) {
        act(name);
        PAD.repeatAt[name] = now + 380;
      } else if (down && REPEATS.has(name) && now >= (PAD.repeatAt[name] || 0)) {
        act(name);
        PAD.repeatAt[name] = now + (name.startsWith("s") && installerRemote() ? 160 : 85);
      }
    }
    if (state.scroll) {
      // The right stick scrolls whatever is on screen, for long pages.
      const root = scopeRoot() === $("app") ? $(`view-${S.tab}`) : null;
      if (root) root.scrollTop += state.scroll * 18;
    }
    PAD.previous = state;
  }
  requestAnimationFrame(padLoop);
}

window.addEventListener("gamepadconnected", (event) => {
  setInput("pad");
  PAD.detected = padFamily(event.gamepad && event.gamepad.id);
  paintGlyphs();
  if (!PAD.connected) {
    toast(PAD.detected === "xbox" ? "Controller connected" : "PlayStation controller connected");
  }
  PAD.connected = true;
});

// ------------------------------------------------------------------ an installer, by controller

function installerRemote() {
  return !!(S.embed && !$("installer").hidden && S.embed.state === "embedded" && !S.embed.popped &&
            $("dialog").hidden && $("keyboard").hidden);
}

// The d-pad moves between the installer's controls (Tab and Shift+Tab, the
// way every Windows wizard is built to be used without a mouse); the stick
// works lists; A presses or ticks; Menu is Enter, which is Next or Install.
const REMOTE_MAP = { up: "prev", down: "next", lb: "prev", rb: "next", left: "left", right: "right",
                     sup: "up", sdown: "down", sleft: "left", sright: "right",
                     a: "press", menu: "enter" };
let remoteChain = Promise.resolve(null);

function remoteAct(name) {
  if (name === "b") { back(); return; }
  if (name === "view") { $("installerPop").click(); return; }
  if (name === "x") { remoteType(); return; }
  const action = REMOTE_MAP[name];
  if (action) sendRemote(action);
}

function sendRemote(action, text = "") {
  remoteChain = remoteChain.then(async () => {
    try {
      const result = await api("/api/embed/remote", { action, text });
      showRemoteFocus(result);
      return result;
    } catch (error) {
      showRemoteFocus({ ok: false, error: error.message });
      return null;
    }
  });
  return remoteChain;
}

function showRemoteFocus(result) {
  const line = $("installerFocus");
  line.classList.toggle("is-warn", !result.ok);
  if (!result.ok) { line.textContent = result.error; return; }
  const focus = result.focus;
  if (!focus) { line.textContent = "Press the d-pad to pick something in the installer."; return; }
  const what = {
    button: "Button",
    checkbox: focus.checked ? "Ticked" : "Not ticked",
    radio: focus.checked ? "Chosen" : "Option",
    edit: "Text box",
    list: "List",
    choice: "Choice",
  }[focus.kind] || "Selected";
  const label = focus.label || (focus.kind === "edit" ? "(empty)" : "");
  line.textContent = label ? `${what}: ${label}` : what;
  if (focus.kind === "edit") {
    const tip = make("span", "installer__tip");
    tip.append(glyph("x"), document.createTextNode("to type"));
    line.append(tip);
  }
}

async function remoteType() {
  const result = await sendRemote("describe");
  const focus = result && result.ok ? result.focus : null;
  if (!focus || focus.kind !== "edit") {
    toast("Pick a text box in the installer with the d-pad first, then press X.", "bad");
    return;
  }
  openKeyboard({
    label: "Type into the installer",
    value: focus.label,
    onInput: null,
    onDone: (value) => {
      sendRemote("type", value);
      focusFirst();
    },
  });
}

// ------------------------------------------------------------------ button icons

// The PlayStation shapes are drawn the way the DualSense prints them: thin,
// even strokes, the same size inside the button, centred by eye (the
// triangle sits a touch low so it looks centred, as on the pad).
const SHAPES = {
  cross: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.1 6.1l7.8 7.8M13.9 6.1l-7.8 7.8" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>',
  circle: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="4.7" fill="none" stroke="currentColor" stroke-width="1.75"/></svg>',
  square: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5.9" y="5.9" width="8.2" height="8.2" fill="none" stroke="currentColor" stroke-width="1.75"/></svg>',
  triangle: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 5.3l4.9 8.6H5.1z" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="miter"/></svg>',
  options: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 6.5h10M5 10h10M5 13.5h10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  create: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 14V6.2M6.4 7.6l1.9 4.6M13.6 7.6l-1.9 4.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  dpad: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7.6 2.8h4.8v4.8h4.8v4.8h-4.8v4.8H7.6v-4.8H2.8V7.6h4.8z" fill="currentColor"/></svg>',
};

const GLYPH_SETS = {
  xbox: { a: "A", b: "B", x: "X", y: "Y", lb: "LB", rb: "RB", menu: "☰", view: "⧉", ls: "LS",
          dpad: SHAPES.dpad },
  ps: { a: SHAPES.cross, b: SHAPES.circle, x: SHAPES.square, y: SHAPES.triangle, lb: "L1", rb: "R1",
        menu: SHAPES.options, view: SHAPES.create, ls: "L3", dpad: SHAPES.dpad },
};

function padStyle() {
  const chosen = (S.data && S.data.settings && S.data.settings.glyphs) || "playstation";
  if (chosen === "xbox") return "xbox";
  if (chosen === "playstation") return PAD.detected === "ps4" ? "ps4" : "ps5";
  return PAD.detected;
}

function fillGlyph(node) {
  const button = node.dataset.btn;
  const style = padStyle();
  const family = style === "xbox" ? "xbox" : "ps";
  let content = GLYPH_SETS[family][button] || button.toUpperCase();
  if (style === "ps4" && button === "view") content = "SHARE";
  if (style === "ps4" && button === "menu") content = "OPTIONS";
  node.className = `glyph glyph--${family} glyph--${family}-${button}` +
    (family === "ps" ? ` glyph--${style === "ps4" ? "ps4" : "ps5"}` : "");
  if (content.startsWith("<svg")) node.innerHTML = content;
  else node.textContent = content;
}

function glyph(button) {
  const node = make("span");
  node.dataset.btn = button;
  node.setAttribute("aria-hidden", "true");
  fillGlyph(node);
  return node;
}

function paintGlyphs() {
  for (const node of document.querySelectorAll(".glyph[data-btn]")) fillGlyph(node);
}

// ------------------------------------------------------------------ hints

function updateHints() {
  const root = scopeRoot();
  let hints;
  if (root === $("dialog")) hints = [["a", "Choose"], ["b", "Cancel"]];
  else if (root === $("keyboard")) hints = [["a", "Type"], ["x", "Delete"], ["y", "Space"], ["menu", "Done"]];
  else if (root === $("installer")) {
    hints = installerRemote()
      ? [["dpad", "Move"], ["a", "Press"], ["menu", "Next / OK"], ["x", "Type"], ["ls", "Lists"], ["b", "Hide"]]
      : [["a", "Select"], ["b", "Hide"]];
  } else if (root === $("detail")) hints = [["a", "Select"], ["b", "Back"]];
  else {
    hints = [["a", S.tab === "discover" ? "Open" : "Select"], ["x", "Search"], ["y", "Downloads"],
             ["menu", "Settings"], ["b", S.tab === "discover" ? "Leave" : "Back"]];
  }
  const box = $("hints");
  box.innerHTML = "";
  for (const [button, label] of hints) {
    const hint = make("span", "hint");
    hint.append(glyph(button), document.createTextNode(label));
    box.append(hint);
  }
}

// ------------------------------------------------------------------ wiring

function wire() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.dataset.key = `tab:${tab.dataset.tab}`;
    tab.addEventListener("click", () => setTab(tab.dataset.tab));
  }
  $("searchBox").dataset.key = "search";
  $("searchBox").addEventListener("click", () => openSearch());
  $("pendingChip").dataset.key = "pending";
  $("pendingChip").addEventListener("click", () => restartFlow(true));
  $("libraryRestart").addEventListener("click", () => restartFlow(true));

  for (const [id, field] of [["optRestart", "restart"], ["optReopen", "reopen"], ["optGlyphs", "glyphs"],
                             ["optSiteMethod", "site_method"], ["optTorrentEngine", "torrent_engine"]]) {
    for (const seg of $(id).querySelectorAll(".seg")) {
      seg.dataset.key = `${id}:${seg.dataset.value}`;
      seg.addEventListener("click", async () => {
        await saveSettings({ [field]: seg.dataset.value });
        if (field === "glyphs") paintGlyphs();
        if (field === "restart" && seg.dataset.value === "now") {
          toast("Steam will restart by itself once installs finish.");
        }
      });
    }
  }
  $("optSeed").dataset.key = "optSeed";
  $("optSeed").addEventListener("click", () => saveSettings({ keep_seeding: !S.data.settings.keep_seeding }));
  $("torrentLogin").dataset.key = "torrentLogin";
  $("torrentLogin").addEventListener("click", torrentLogin);
  $("torrentLook").dataset.key = "torrentLook";
  $("torrentLook").addEventListener("click", async () => {
    $("torrentLook").disabled = true;
    try {
      const found = await api("/api/store/torrent-client", {});
      S.data.settings.torrent = found;
      renderSettings();
      toast(found.message, found.label && !found.needs_login ? "good" : "bad");
      if (found.needs_login) torrentLogin();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      $("torrentLook").disabled = false;
    }
  });
  for (const seg of $("optParallel").querySelectorAll(".seg")) {
    seg.dataset.key = `optParallel:${seg.dataset.value}`;
    seg.addEventListener("click", () => saveSettings({ parallel: Number(seg.dataset.value) }));
  }
  const PAGE_STEPS = [1, 2, 3, 5, 10, 15, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000];
  const stepPages = async (direction) => {
    const now = Number(S.data.settings.site_pages) || 10;
    const index = PAGE_STEPS.findIndex((step) => step >= now);
    const at = index < 0 ? PAGE_STEPS.length - 1 : index;
    const next = PAGE_STEPS[Math.max(0, Math.min(PAGE_STEPS.length - 1,
      PAGE_STEPS[at] === now ? at + direction : (direction > 0 ? at : at - 1)))];
    if (next !== now && await saveSettings({ site_pages: next })) loadOverview();
  };
  $("sitePagesLess").dataset.key = "sitePagesLess";
  $("sitePagesLess").addEventListener("click", () => stepPages(-1));
  $("sitePagesMore").dataset.key = "sitePagesMore";
  $("sitePagesMore").addEventListener("click", () => stepPages(1));
  $("optAllPages").dataset.key = "optAllPages";
  $("optAllPages").addEventListener("click", async () => {
    const every = Number(S.data.settings.site_pages) === 0;
    if (await saveSettings({ site_pages: every ? 10 : 0 })) {
      if (!every) toast("Reading the whole site in the background. Games appear as pages arrive.");
      loadOverview();
    }
  });
  $("optMute").dataset.key = "optMute";
  $("optMute").addEventListener("click", () => saveSettings({ mute_installers: S.data.settings.mute_installers === false }));
  $("optOptional").dataset.key = "optOptional";
  $("optOptional").addEventListener("click", () => saveSettings({ optional_files: !S.data.settings.optional_files }));
  $("optKeep").dataset.key = "optKeep";
  $("optKeep").addEventListener("click", () => saveSettings({ keep_downloads: !S.data.settings.keep_downloads }));
  $("addSelf").dataset.key = "addSelf";
  $("addSelf").addEventListener("click", async () => {
    try {
      const result = await api("/api/store/add-self", {});
      if (result.outcome === "added") S.data.store_shortcut = true;
      toast(result.message);
      applyStatus(await api("/api/store/status"), true);
      renderSettings();
    } catch (error) {
      toast(error.message, "bad");
    }
  });
  $("catalogueSource").dataset.key = "catSource";
  $("catalogueCheck").dataset.key = "catCheck";
  $("catalogueCheck").addEventListener("click", checkCatalogue);
  $("catalogueAdd").dataset.key = "catAdd";
  $("catalogueAdd").addEventListener("click", addCatalogue);
  $("catalogueRescan").dataset.key = "catRescan";
  $("catalogueRescan").addEventListener("click", () => {
    ask("Read everything again?",
      "Every catalogue is read again from the start. Release websites are re-read in the background, " +
      "page by page, so the shelf stays usable meanwhile; every game is refreshed, and ones a site " +
      "has taken down are removed once all of it has been read. Artwork not found before is looked for again.", [
        { label: "Read everything again", primary: true, run: async () => {
          try {
            applyOverview(await api("/api/store/refresh-everything", {}));
            toast("Reading everything again in the background.");
          } catch (error) {
            toast(error.message, "bad");
          }
        } },
        { label: "Not now", run: () => {} },
      ]);
  });
  $("catalogueRefresh").dataset.key = "catRefresh";
  $("catalogueRefresh").addEventListener("click", async () => {
    $("catalogueRefresh").disabled = true;
    try {
      applyOverview(await api("/api/store/refresh", {}));
      toast("Catalogues refreshed");
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      $("catalogueRefresh").disabled = false;
    }
  });
  $("leaveButton").dataset.key = "leave";
  $("leaveButton").addEventListener("click", leave);
  $("installerHide").addEventListener("click", () => back());
  $("installerPop").addEventListener("click", async () => {
    try {
      const { embed } = await api(S.embed && S.embed.popped ? "/api/embed/bringback" : "/api/embed/popout", {});
      renderInstaller(embed);
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  window.addEventListener("resize", () => { S.embedKey = ""; syncEmbed(); });
  document.addEventListener("visibilitychange", syncEmbed);
  document.addEventListener("focusin", (event) => {
    const view = event.target.closest && event.target.closest(".view");
    if (view && event.target.dataset && event.target.dataset.key) S.memory[view.id] = event.target.dataset.key;
  });

  const clock = () => {
    $("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  clock();
  setInterval(clock, 15000);
}

async function start() {
  wire();
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("is-active", tab.dataset.tab === S.tab);
  updateHints();
  try {
    await loadOverview();
  } catch (error) {
    toast(error.message, "bad");
  }
  focusFirst();
  poll();
  requestAnimationFrame(padLoop);
}

start();
