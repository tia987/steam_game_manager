/* Non-Steam Game Manager - client */

const $ = (id) => document.getElementById(id);

const S = {
  boot: null,
  settings: {},
  games: [],
  matches: {},        // uid -> { sgdb_id, sgdb_name, thumb, url }
  overrides: {},      // uid -> { sgdb_id, art: {kind: url}, launch_mode }
  selected: new Set(),
  managed: [],
  managedPicked: new Set(),
  filter: "",
  tab: "library",
  artUid: null,
  artKind: "capsule",
  browseResolve: null,
  browseMode: "dir",
  browsePath: "",
  editingParser: null,
  feeds: [],
  feedItems: [],
  feedFilter: "",
  feedTag: "All",
  programs: [],
  programFilter: "",
  sweepProgram: null,
  grabbed: null,
  catalog: [],
  catalogTag: "All",
  catalogFilter: "",
  releases: {},
  resolved: null,
  watched: [],
  getKind: "all",
  getFilter: "",
  getReleases: [],
  getUrlUsed: "",
  getForce: false,
  filesPages: 25,
  cameFromIndex: false,
  indexPrograms: [],
  indexFilter: "",
  indexSite: "",
  indexUrl: "",
  indexPages: 25,
  indexExhausted: true,
  incoming: [],
  incomingFolder: "",
  watchTimer: null,
  installing: "",
};

const ART_LABELS = {
  capsule: "Cover 600\u00d7900",
  wide: "Wide capsule",
  hero: "Hero banner",
  logo: "Logo",
  icon: "Icon",
};

/* ----------------------------------------------------------------- api */

async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function toast(message, tone = "good") {
  const node = $("toast");
  node.textContent = message;
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

/* Runs a background job and streams its progress into the dialog. */
function runJob(jobId, title, { silent = false } = {}) {
  let peak = 0;
  return new Promise((resolve, reject) => {
    const dialog = $("jobDialog");
    if (!silent) {
      $("jobTitle").textContent = title;
      $("jobFill").style.width = "0%";
      $("jobMessage").textContent = "";
      $("jobLog").textContent = "";
      $("jobClose").disabled = true;
      peak = 0;
      if (!dialog.open) dialog.showModal();
    }
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/job?id=${jobId}`);
      } catch (error) {
        if (!silent) dialog.close();
        return reject(error);
      }
      if (!silent) {
        // total = 0 means the work has no knowable size yet: a crawl finds
        // out how many pages exist by reading them. Rather than invent a
        // denominator and keep revising it upward, the bar just shows
        // movement until the count is real.
        const meter = $("jobFill");
        if (!job.total && !job.finished) {
          meter.classList.add("is-seeking");
          meter.style.width = "100%";
        } else {
          meter.classList.remove("is-seeking");
          const raw = job.total ? Math.round((job.done / job.total) * 100) : 100;
          peak = job.finished ? 100 : Math.min(Math.max(peak, raw), 97);
          meter.style.width = `${peak}%`;
        }
        $("jobMessage").textContent = job.message || job.stage;
        $("jobLog").textContent = (job.log || []).join("\n");
        $("jobLog").scrollTop = $("jobLog").scrollHeight;
      }
      if (!job.finished) return setTimeout(tick, 400);
      if (!silent) {
        $("jobClose").disabled = false;
        $("jobFill").style.width = "100%";
      }
      if (job.error) {
        if (!silent) $("jobMessage").textContent = job.error;
        return reject(new Error(job.error));
      }
      resolve(job);
    };
    tick();
  });
}

/* ----------------------------------------------------------- bootstrap */

async function boot() {
  S.boot = await api("/api/bootstrap");
  S.settings = S.boot.settings;

  const account = $("account");
  account.innerHTML = "";
  S.boot.users.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.id3;
    option.textContent = user.name;
    account.append(option);
  });
  if (!S.boot.users.length) {
    account.innerHTML = "<option>No account found</option>";
  }

  const state = $("steamState");
  if (!S.boot.steam_root) {
    state.dataset.state = "missing";
    state.textContent = "Steam not found";
  } else if (S.boot.steam_running) {
    state.dataset.state = "running";
    state.textContent = "Steam is open, close it first";
  } else {
    state.dataset.state = "closed";
    state.textContent = "Steam is closed, ready";
  }

  renderSources();
  renderFolders();
  renderParsers();
  renderArtKindChecks();

  $("apiKey").value = S.settings.api_key || "";
  $("allowAnimated").checked = !!S.settings.allow_animated;
  $("allowNsfw").checked = !!S.settings.allow_nsfw;
  $("addTag").checked = S.settings.add_tag !== false;
  $("githubToken").value = S.settings.github_token || "";
  $("overwriteArt").checked = !!S.settings.overwrite_art;
  $("preferDirect").checked = !!S.settings.prefer_direct;
  $("configPath").textContent = `Settings live in ${S.boot.config_dir}`;

  const presets = await api("/api/presets");
  const select = $("pPreset");
  select.innerHTML = '<option value="">Blank</option>';
  presets.presets.forEach((preset, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = preset.name;
    select.append(option);
  });
  select._presets = presets.presets;
}

function renderSources() {
  const host = $("sourceList");
  host.innerHTML = "";
  const active = new Set(S.settings.sources || []);
  S.boot.sources.forEach((source) => {
    const label = document.createElement("label");
    label.className = "check";
    label.innerHTML = `<input type="checkbox" value="${source.key}"><span>${source.label}</span>`;
    const box = label.querySelector("input");
    box.checked = active.has(source.key);
    box.addEventListener("change", () => {
      const chosen = [...host.querySelectorAll("input:checked")].map((i) => i.value);
      saveSettings({ sources: chosen });
    });
    host.append(label);
  });
}

function renderFolders() {
  const host = $("folderList");
  host.innerHTML = "";
  (S.settings.folders || []).forEach((folder, index) => {
    const item = document.createElement("li");
    item.className = "row";
    item.innerHTML = `<span class="row__text">
        <span class="row__name">${escapeHtml(baseName(folder.path))}</span>
        <span class="row__sub">${escapeHtml(folder.path)}</span>
      </span>
      <button class="row__button" type="button" title="Remove">&times;</button>`;
    item.querySelector("button").addEventListener("click", () => {
      const next = [...(S.settings.folders || [])];
      next.splice(index, 1);
      saveSettings({ folders: next }).then(renderFolders);
    });
    host.append(item);
  });
  if (!(S.settings.folders || []).length) {
    host.innerHTML = '<li class="panel__hint">No folders added.</li>';
  }
}

const INCOMING_STATE = {
  ready: ["ready", "var(--ready)"],
  archive: ["packed", "var(--warn)"],
  installer: ["installer", "var(--attention)"],
  image: ["disc image", "var(--dim)"],
  unknown: ["unclear", "var(--dimmer)"],
};

async function loadIncoming(folder) {
  try {
    const data = await api("/api/incoming", { folder: folder || S.incomingFolder });
    S.incomingFolder = data.folder;
    S.incoming = data.items || [];
    S.settings.incoming_folder = data.folder;
    renderIncoming();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function renderIncoming() {
  const host = $("incomingList");
  $("incomingPath").textContent = S.incomingFolder || "";
  host.innerHTML = "";
  const ready = S.incoming.filter((i) => i.state === "ready");
  $("incomingStage").hidden = ready.length === 0;
  $("incomingStage").textContent =
    `Stage ${ready.length} ready item${ready.length === 1 ? "" : "s"}`;

  if (!S.incoming.length) {
    host.innerHTML = '<li class="panel__hint">Nothing waiting.</li>';
    return;
  }
  S.incoming.forEach((item) => {
    const [label, colour] = INCOMING_STATE[item.state] || INCOMING_STATE.unknown;
    const row = document.createElement("li");
    row.className = "row";
    row.innerHTML = `<span class="row__text">
        <span class="row__name">${escapeHtml(item.name)}</span>
        <span class="row__sub" style="color:${colour}">${label} \u00b7 ${humanSize(item.size)}</span>
      </span>`;
    if (item.state === "archive") {
      const button = document.createElement("button");
      button.className = "row__button";
      button.type = "button";
      button.textContent = "Unpack";
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "\u2026";
        try {
          await api("/api/incoming/unpack", { path: item.path });
          await loadIncoming();
          toast(`Unpacked ${item.name}`);
        } catch (error) {
          toast(error.message, "bad");
          button.disabled = false;
          button.textContent = "Unpack";
        }
      });
      row.append(button);
    } else if (item.state === "installer") {
      row.title = "Installers are not run for you. Open the folder and run it yourself.";
    }
    host.append(row);
  });
}

/* Noticing a finished download, then what its installer left behind. */

function stopWatching() {
  if (S.watchTimer) clearInterval(S.watchTimer);
  S.watchTimer = null;
}

function startWatching() {
  if (!S.incomingFolder) {
    $("incomingWatch").checked = false;
    return toast("Choose your downloads folder first", "bad");
  }
  stopWatching();
  const tick = async () => {
    try {
      const data = await api("/api/incoming/poll", { folder: S.incomingFolder });
      (data.finished || []).forEach(announceFinished);
      if (data.finished && data.finished.length) loadIncoming();
    } catch (error) {
      // A folder that vanished should stop the watch, not spam the screen.
      stopWatching();
      $("incomingWatch").checked = false;
      toast(error.message, "bad");
    }
  };
  tick();
  S.watchTimer = setInterval(tick, 15000);
  toast("Watching for finished downloads");
}

function announceFinished(item) {
  if (item.state === "installer" || item.installer) {
    toast(`${item.name} finished downloading`);
    if (confirm(`${item.name} has finished.\n\nStart its installer now? ` +
                `You choose where it goes; nothing is installed for you.`)) {
      startInstaller(item);
    }
  } else {
    toast(`${item.name} finished and is ready to stage`);
  }
}

async function startInstaller(item) {
  try {
    await api("/api/incoming/install", { path: item.path });
    S.installing = item.name;
    $("incomingInstalled").hidden = false;
    toast(`${item.name}: installer started. Press the button when it finishes.`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function collectInstalled() {
  try {
    const { games } = await api("/api/incoming/installed", {});
    if (!games.length) {
      return toast("Nothing new found yet. Finish the installer first.", "bad");
    }
    const data = await api("/api/games", {});
    S.games = data.games;
    games.forEach((g) => S.selected.add(g.uid));
    renderWall();
    $("incomingInstalled").hidden = true;
    S.installing = "";
    toast(`${games.length} newly installed \u2014 pick covers, then Add to Steam`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function stageIncoming() {
  const ready = S.incoming.filter((i) => i.state === "ready");
  if (!ready.length) return toast("Nothing ready yet", "bad");
  try {
    const result = await api("/api/incoming/stage", { items: ready });
    const data = await api("/api/games", {});
    S.games = data.games;
    (result.games || []).forEach((g) => S.selected.add(g.uid));
    renderWall();
    toast(`${result.staged} staged \u2014 pick covers, then Add to Steam`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

function renderParsers() {
  const host = $("parserList");
  host.innerHTML = "";
  (S.settings.parsers || []).forEach((parser) => {
    const item = document.createElement("li");
    item.className = "row";
    item.innerHTML = `<input type="checkbox" ${parser.enabled !== false ? "checked" : ""}>
      <span class="row__text">
        <span class="row__name">${escapeHtml(parser.name)}</span>
        <span class="row__sub">${escapeHtml(parser.rom_dir || "no folder set")}</span>
      </span>
      <button class="row__button" type="button" title="Edit">Edit</button>`;
    item.querySelector("input").addEventListener("change", (event) => {
      const next = (S.settings.parsers || []).map((p) =>
        p.id === parser.id ? { ...p, enabled: event.target.checked } : p);
      saveSettings({ parsers: next });
    });
    item.querySelector("button").addEventListener("click", () => openParser(parser));
    host.append(item);
  });
  if (!(S.settings.parsers || []).length) {
    host.innerHTML = '<li class="panel__hint">No ROM systems yet.</li>';
  }
}

function renderArtKindChecks() {
  const host = $("artKindChecks");
  host.innerHTML = "";
  const active = new Set(S.settings.art_kinds || []);
  S.boot.art_kinds.forEach((kind) => {
    const label = document.createElement("label");
    label.className = "check";
    label.innerHTML = `<input type="checkbox" value="${kind}"><span>${ART_LABELS[kind] || kind}</span>`;
    label.querySelector("input").checked = active.has(kind);
    host.append(label);
  });
}

async function saveSettings(patch) {
  const result = await api("/api/settings", { settings: patch });
  S.settings = result.settings;
  return S.settings;
}

/* --------------------------------------------------------------- utils */

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const baseName = (path) => String(path).split(/[\\/]/).filter(Boolean).pop() || path;

function hueOf(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) % 360;
  return hash;
}

function sourceLabel(game) {
  if (game.source.startsWith("rom:")) {
    const parser = (S.settings.parsers || []).find((p) => `rom:${p.id}` === game.source);
    return parser ? parser.name : "ROM";
  }
  const match = (S.boot.sources || []).find((s) => s.key === game.source);
  return match ? match.label : game.source;
}

/* ---------------------------------------------------------------- wall */

function visibleGames() {
  const needle = S.filter.trim().toLowerCase();
  if (!needle) return S.games;
  return S.games.filter((game) =>
    game.title.toLowerCase().includes(needle) ||
    sourceLabel(game).toLowerCase().includes(needle));
}

function coverFor(game) {
  const override = S.overrides[game.uid];
  if (override && override.art && override.art.capsule) return override.art.capsule;
  const match = S.matches[game.uid];
  return match && match.thumb ? match.thumb : "";
}

function renderWall() {
  const wall = $("wall");
  const games = visibleGames();
  wall.innerHTML = "";

  $("empty").hidden = S.games.length > 0;
  wall.hidden = S.games.length === 0;
  $("wallSummary").hidden = S.games.length === 0;

  if (S.games.length) {
    const matched = S.games.filter((g) => coverFor(g)).length;
    $("wallSummary").textContent =
      `${S.games.length} games found, ${matched} with a cover. ` +
      `Click a cover to select it, or the Art button to choose a different one.`;
  }

  games.forEach((game, index) => {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.uid = game.uid;
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.setAttribute("aria-pressed", S.selected.has(game.uid) ? "true" : "false");
    card.style.animationDelay = `${Math.min(index, 40) * 14}ms`;

    const cover = coverFor(game);
    card.dataset.matched = cover ? "yes" : "no";
    const title = escapeHtml(game.title);

    card.innerHTML = `
      <div class="card__art">
        <span class="card__check" aria-hidden="true"></span>
        <button class="card__edit" type="button">Art</button>
        ${cover
          ? `<img src="${escapeHtml(cover)}" alt="" loading="lazy">`
          : `<div class="cover-blank" style="--hue:${hueOf(game.title)}"><span>${title}</span></div>`}
      </div>
      <div class="card__meta">
        <p class="card__title">${title}</p>
        <p class="card__source" title="${escapeHtml(game.detail || game.exe || "")}">${escapeHtml(sourceLabel(game))}</p>
      </div>`;

    // Hovering the whole card shows where the entry came from, which is how
    // you check a surprising source without opening the registry.
    card.title = game.detail || game.exe || "";
    card.addEventListener("click", (event) => {
      if (event.target.classList.contains("card__edit")) return;
      toggle(game.uid);
    });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle(game.uid);
      }
    });
    card.querySelector(".card__edit").addEventListener("click", (event) => {
      event.stopPropagation();
      openArt(game);
    });

    wall.append(card);
  });

  updateDock();
}

function toggle(uid) {
  if (S.selected.has(uid)) S.selected.delete(uid);
  else S.selected.add(uid);
  const card = document.querySelector(`.card[data-uid="${uid}"]`);
  if (card) card.setAttribute("aria-pressed", S.selected.has(uid) ? "true" : "false");
  updateDock();
}

function allVisibleSelected() {
  const visible = visibleGames();
  return visible.length > 0 && visible.every((game) => S.selected.has(game.uid));
}

function toggleSelectAll() {
  // One button that says what it will do. Selecting everything and then
  // hunting for a way to undo it was a gap.
  const visible = visibleGames();
  if (allVisibleSelected()) visible.forEach((game) => S.selected.delete(game.uid));
  else visible.forEach((game) => S.selected.add(game.uid));
  renderWall();
}

function updateSelectAllLabel() {
  const button = $("selectAll");
  if (!button) return;
  const visible = visibleGames();
  button.disabled = visible.length === 0;
  button.textContent = allVisibleSelected()
    ? (S.filter.trim() ? "Deselect these" : "Deselect all")
    : (S.filter.trim() ? "Select these" : "Select all");
}

function updateDock() {
  updateSelectAllLabel();
  if (["feeds", "uninstall", "catalog", "get"].includes(S.tab)) { $("dock").hidden = true; return; }
  const count = S.tab === "library" ? S.selected.size : S.managedPicked.size;
  $("dock").hidden = count === 0;
  $("dockNumber").textContent = String(count);
  $("dockLabel").textContent = count === 1 ? "game selected" : "games selected";
  $("applyButton").hidden = S.tab !== "library";
}

/* ---------------------------------------------------------------- scan */

async function scan() {
  const button = $("scanButton");
  button.disabled = true;
  button.textContent = "Scanning\u2026";
  try {
    const { job } = await api("/api/scan", {});
    const finished = await runJob(job, "Scanning for games");
    const data = await api("/api/games", {});
    S.games = data.games;
    S.selected = new Set(S.games.map((g) => g.uid));
    S.matches = {};
    S.overrides = {};
    renderWall();
    const notes = (finished.result && finished.result.notes) || [];
    $("scanNotes").hidden = notes.length === 0;
    $("scanNotes").textContent = notes.join("\n");
    $("jobDialog").close();
    toast(`${S.games.length} games found`);
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "Scan for games";
  }
}

async function matchArt() {
  if (!S.settings.api_key) {
    $("settingsDialog").showModal();
    toast("Add your SteamGridDB API key first", "bad");
    return;
  }
  const uids = S.selected.size ? [...S.selected] : S.games.map((g) => g.uid);
  if (!uids.length) return toast("Scan for games first", "bad");
  try {
    const { job } = await api("/api/match", { uids });
    const finished = await runJob(job, "Looking up covers");
    S.matches = { ...S.matches, ...(finished.result.matches || {}) };
    renderWall();
    $("jobDialog").close();
    toast(finished.message);
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ------------------------------------------------------------ art picker */

async function openArt(game) {
  S.artUid = game.uid;
  S.artKind = "capsule";
  $("artTitle").textContent = game.title;
  $("artSearch").value = game.sgdb_hint || game.title;
  $("artGrid").innerHTML = "";
  $("artResults").innerHTML = "";

  const kinds = $("artKinds");
  kinds.innerHTML = "";
  (S.boot.art_kinds || []).forEach((kind) => {
    const button = document.createElement("button");
    button.className = `segment${kind === S.artKind ? " is-active" : ""}`;
    button.textContent = ART_LABELS[kind] || kind;
    button.addEventListener("click", () => {
      S.artKind = kind;
      [...kinds.children].forEach((child) => child.classList.remove("is-active"));
      button.classList.add("is-active");
      loadArt();
    });
    kinds.append(button);
  });

  $("artDialog").showModal();
  await searchGames();
}

async function searchGames() {
  const host = $("artResults");
  host.innerHTML = '<span class="panel__hint">Searching\u2026</span>';
  try {
    const { results } = await api("/api/sgdb/search", { term: $("artSearch").value });
    host.innerHTML = "";
    if (!results.length) {
      host.innerHTML = '<span class="panel__hint">No games matched that name.</span>';
      return;
    }
    const current = currentGameId();
    results.slice(0, 12).forEach((result, index) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = result.name;
      const active = current ? result.id === current : index === 0;
      if (active) chip.classList.add("is-active");
      chip.addEventListener("click", () => {
        setOverride(S.artUid, { sgdb_id: result.id });
        [...host.children].forEach((child) => child.classList.remove("is-active"));
        chip.classList.add("is-active");
        loadArt();
      });
      host.append(chip);
    });
    if (!current && results.length) setOverride(S.artUid, { sgdb_id: results[0].id });
    loadArt();
  } catch (error) {
    host.innerHTML = `<span class="notes">${escapeHtml(error.message)}</span>`;
  }
}

function currentGameId() {
  const override = S.overrides[S.artUid];
  if (override && override.sgdb_id) return override.sgdb_id;
  const match = S.matches[S.artUid];
  return match && match.sgdb_id ? match.sgdb_id : null;
}

async function loadArt() {
  const gameId = currentGameId();
  const grid = $("artGrid");
  if (!gameId) { grid.innerHTML = '<p class="panel__hint">Pick a game above.</p>'; return; }
  grid.innerHTML = '<p class="panel__hint">Loading artwork\u2026</p>';
  try {
    const { art } = await api("/api/sgdb/art", { game_id: gameId, kind: S.artKind });
    grid.innerHTML = "";
    if (!art.length) {
      grid.innerHTML = '<p class="panel__hint">No artwork of this kind yet.</p>';
      return;
    }
    const chosen = (S.overrides[S.artUid] || {}).art || {};
    art.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      if (chosen[S.artKind] === item.url) button.classList.add("is-active");
      button.innerHTML = `<img src="${escapeHtml(item.thumb)}" alt="" loading="lazy">`;
      button.title = item.author ? `by ${item.author}` : "";
      button.addEventListener("click", () => {
        const picked = { ...((S.overrides[S.artUid] || {}).art || {}) };
        picked[S.artKind] = item.url;
        setOverride(S.artUid, { art: picked });
        [...grid.children].forEach((child) => child.classList.remove("is-active"));
        button.classList.add("is-active");
        if (S.artKind === "capsule") renderWall();
      });
      grid.append(button);
    });
  } catch (error) {
    grid.innerHTML = `<p class="notes">${escapeHtml(error.message)}</p>`;
  }
}

function setOverride(uid, patch) {
  S.overrides[uid] = { ...(S.overrides[uid] || {}), ...patch };
}

/* --------------------------------------------------------------- apply */

async function apply() {
  if (!S.boot.users.length) return toast("No Steam account found", "bad");
  const games = [...S.selected].map((uid) => {
    const override = S.overrides[uid] || {};
    return { uid, sgdb_id: override.sgdb_id || (S.matches[uid] || {}).sgdb_id, art: override.art };
  });
  if (!games.length) return toast("Select at least one game", "bad");

  try {
    const { job } = await api("/api/apply", {
      user: $("account").value,
      games,
      settings: {
        overwrite_art: $("overwriteArt").checked,
        prefer_direct: $("preferDirect").checked,
      },
    });
    const finished = await runJob(job, `Adding ${games.length} games to Steam`);
    const result = finished.result || {};
    $("jobMessage").textContent =
      `${result.added} added, ${result.updated} updated, ${result.artwork} images saved. ` +
      `Start Steam to see them.`;
    toast("Added. Start Steam to see your games.");
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ------------------------------------------------------------- managed */

async function loadManaged() {
  const wall = $("managedWall");
  wall.innerHTML = '<p class="panel__hint">Loading\u2026</p>';
  try {
    const { games } = await api(`/api/managed?user=${encodeURIComponent($("account").value)}`);
    S.managed = games;
    S.managedPicked = new Set();
    wall.innerHTML = "";
    $("managedSummary").textContent = games.length
      ? `${games.length} shortcuts added by this tool. Select any to remove.`
      : "Nothing added yet.";
    games.forEach((game, index) => {
      const card = document.createElement("article");
      card.className = "card";
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.setAttribute("aria-pressed", "false");
      card.style.animationDelay = `${Math.min(index, 40) * 14}ms`;
      const cover = game.art.capsule || "";
      card.innerHTML = `
        <div class="card__art">
          <span class="card__check" aria-hidden="true"></span>
          ${cover
            ? `<img src="${escapeHtml(cover)}" alt="" loading="lazy">`
            : `<div class="cover-blank" style="--hue:${hueOf(game.title)}"><span>${escapeHtml(game.title)}</span></div>`}
        </div>
        <div class="card__meta">
          <p class="card__title">${escapeHtml(game.title)}</p>
          <p class="card__source">${escapeHtml(game.added)}</p>
        </div>`;
      const pick = () => {
        if (S.managedPicked.has(game.short_id)) S.managedPicked.delete(game.short_id);
        else S.managedPicked.add(game.short_id);
        card.setAttribute("aria-pressed", S.managedPicked.has(game.short_id) ? "true" : "false");
        updateDock();
      };
      card.addEventListener("click", pick);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); }
      });
      wall.append(card);
    });
  } catch (error) {
    wall.innerHTML = `<p class="notes">${escapeHtml(error.message)}</p>`;
  }
}

async function removeManaged() {
  if (!S.managedPicked.size) return toast("Select the games to remove", "bad");
  try {
    const { job } = await api("/api/remove", {
      user: $("account").value,
      short_ids: [...S.managedPicked],
      delete_art: true,
    });
    const finished = await runJob(job, "Removing shortcuts");
    toast(finished.message || "Removed");
    $("jobDialog").close();
    loadManaged();
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* -------------------------------------------------------------- browse */

function openBrowse(title, mode, startPath) {
  S.browseMode = mode;
  $("browseTitle").textContent = title;
  $("browsePick").hidden = mode !== "dir";
  loadBrowse(startPath || "");
  $("browseDialog").showModal();
  return new Promise((resolve) => { S.browseResolve = resolve; });
}

async function loadBrowse(path) {
  const list = $("browseList");
  list.innerHTML = '<li><button type="button">Loading\u2026</button></li>';
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}&files=${S.browseMode === "file" ? 1 : 0}`);
    S.browsePath = data.path;
    $("browsePath").textContent = data.path;
    $("browseUp").disabled = !data.parent;
    $("browseUp").onclick = () => loadBrowse(data.parent);

    const drives = $("browseDrives");
    drives.innerHTML = "";
    (data.drives || []).forEach((drive) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = drive;
      chip.addEventListener("click", () => loadBrowse(drive));
      drives.append(chip);
    });

    list.innerHTML = "";
    data.dirs.forEach((dir) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `\u{1F4C1}  ${dir.name}`;
      button.addEventListener("click", () => loadBrowse(dir.path));
      item.append(button);
      list.append(item);
    });
    (data.files || []).forEach((file) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "is-file";
      button.textContent = file.name;
      button.addEventListener("click", () => finishBrowse(file.path));
      item.append(button);
      list.append(item);
    });
    if (!list.children.length) {
      list.innerHTML = '<li><button type="button" disabled>Nothing here</button></li>';
    }
  } catch (error) {
    list.innerHTML = `<li><button type="button" disabled>${escapeHtml(error.message)}</button></li>`;
  }
}

function finishBrowse(value) {
  $("browseDialog").close();
  if (S.browseResolve) S.browseResolve(value);
  S.browseResolve = null;
}

/* -------------------------------------------------------- rom parsers */

function openParser(parser) {
  S.editingParser = parser ? { ...parser } : null;
  const p = parser || {};
  $("pName").value = p.name || "";
  $("pDir").value = p.rom_dir || "";
  $("pExt").value = p.extensions || "";
  $("pEmu").value = p.emulator || "";
  $("pArgs").value = p.args || '"{rom}"';
  $("pRecursive").checked = p.recursive !== false;
  $("pMulti").checked = p.group_multidisc !== false;
  $("pPreset").value = "";
  $("pDelete").hidden = !parser;
  $("parserDialog").showModal();
}

function applyPreset() {
  const select = $("pPreset");
  const preset = (select._presets || [])[Number(select.value)];
  if (!preset) return;
  $("pName").value = preset.name;
  $("pExt").value = preset.extensions;
  const emulator = $("pEmu").value.trim();
  if (preset.core && emulator) {
    const sep = emulator.includes("\\") ? "\\" : "/";
    const root = emulator.split(/[\\/]/).slice(0, -1).join(sep);
    const ext = emulator.toLowerCase().endsWith(".exe") ? "dll" : "so";
    $("pArgs").value = `-L "${root}${sep}cores${sep}${preset.core}_libretro.${ext}" "{rom}"`;
  } else if (!$("pArgs").value.trim()) {
    $("pArgs").value = '"{rom}"';
  }
}

async function saveParser() {
  const name = $("pName").value.trim();
  if (!name) return toast("Give the system a name", "bad");
  const parsers = [...(S.settings.parsers || [])];
  const draft = {
    ...(S.editingParser || (await api("/api/parser/blank", {})).parser),
    name,
    rom_dir: $("pDir").value.trim(),
    extensions: $("pExt").value.trim(),
    emulator: $("pEmu").value.trim(),
    args: $("pArgs").value.trim() || '"{rom}"',
    recursive: $("pRecursive").checked,
    group_multidisc: $("pMulti").checked,
    tags: [name],
    enabled: true,
  };
  const index = parsers.findIndex((p) => p.id === draft.id);
  if (index >= 0) parsers[index] = draft;
  else parsers.push(draft);
  await saveSettings({ parsers });
  renderParsers();
  $("parserDialog").close();
  toast(`${name} saved`);
}

/* --------------------------------------------------------------- feeds */

function relativeTime(seconds) {
  if (!seconds) return "undated";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 3600) return `${Math.max(1, Math.round(delta / 60))}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  if (delta < 604800) return `${Math.round(delta / 86400)}d ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}

function renderFeedTags() {
  const host = $("feedTags");
  const tags = [...new Set(S.feedItems.map((i) => i.tag).filter(Boolean))].sort();
  host.innerHTML = "";
  ["All", ...tags].forEach((tag) => {
    const chip = document.createElement("button");
    chip.className = `chip${S.feedTag === tag ? " is-active" : ""}`;
    chip.textContent = tag;
    chip.addEventListener("click", () => { S.feedTag = tag; renderFeeds(); });
    host.append(chip);
  });
}

function renderFeeds() {
  renderFeedTags();
  const host = $("feedList");
  const needle = S.feedFilter.trim().toLowerCase();
  const items = S.feedItems.filter((item) => {
    if (S.feedTag !== "All" && item.tag !== S.feedTag) return false;
    if (!needle) return true;
    return `${item.title} ${item.summary} ${item.feed}`.toLowerCase().includes(needle);
  });

  host.innerHTML = "";
  if (!items.length) {
    host.innerHTML = S.feedItems.length
      ? '<li class="panel__hint">Nothing matches that filter.</li>'
      : '<li class="panel__hint">Press Refresh to pull in the latest.</li>';
    return;
  }

  items.slice(0, 250).forEach((item, index) => {
    const row = document.createElement("li");
    row.className = `feeditem${item.image ? "" : " feeditem--plain"}`;
    row.style.animationDelay = `${Math.min(index, 30) * 10}ms`;
    const safeLink = /^https?:\/\//i.test(item.link || "") ? item.link : "";
    row.innerHTML = `
      ${item.image ? `<div class="feeditem__thumb"><img src="${escapeHtml(item.image)}" alt="" loading="lazy"></div>` : ""}
      <div>
        <p class="feeditem__eyebrow">
          ${item.tag ? `<span class="feeditem__tag" data-tag="${escapeHtml(item.tag)}">${escapeHtml(item.tag)}</span>` : ""}
          <span>${escapeHtml(item.feed)}</span>
          <span>${relativeTime(item.published)}</span>
          ${item.owned ? '<span class="feeditem__owned">In your library</span>' : ""}
          ${/\.(exe|msi|zip|7z|rar|appimage|deb|rpm|dmg|pkg)$/i.test(safeLink)
            ? '<button class="feeditem__get" type="button">Download</button>' : ""}
        </p>
        <p class="feeditem__title">${safeLink
          ? `<a href="${escapeHtml(safeLink)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>`
          : escapeHtml(item.title)}</p>
        ${item.summary ? `<p class="feeditem__summary">${escapeHtml(item.summary)}</p>` : ""}
      </div>`;
    const grab = row.querySelector(".feeditem__get");
    if (grab) grab.addEventListener("click", () => openGrab(safeLink));
    host.append(row);
  });

  $("feedSummary").textContent =
    `${items.length} of ${S.feedItems.length} headlines. Every link opens on the original site.`;
}

async function loadFeeds(refresh) {
  try {
    if (refresh) {
      const { job } = await api("/api/feeds/refresh", { user: $("account").value });
      const finished = await runJob(job, "Checking feeds");
      S.feedItems = (finished.result || {}).items || [];
      const notes = (finished.result || {}).notes || [];
      $("feedNotes").hidden = notes.length === 0;
      $("feedNotes").textContent = notes.join("\n");
      $("jobDialog").close();
      toast(finished.message);
    } else {
      const data = await api(`/api/feeds?user=${encodeURIComponent($("account").value)}`);
      S.feedItems = data.items || [];
      S.feeds = data.feeds || [];
    }
    renderFeeds();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function renderFeedRows() {
  const host = $("feedRows");
  host.innerHTML = "";
  S.feeds.forEach((feed, index) => {
    const row = document.createElement("li");
    row.className = "row";
    row.innerHTML = `<input type="checkbox" ${feed.enabled ? "checked" : ""}>
      <span class="row__text">
        <span class="row__name">${escapeHtml(feed.name)}</span>
        <span class="row__sub">${escapeHtml(feed.url)}</span>
      </span>
      <input class="tagfield" type="text" list="tagOptions" placeholder="Tag"
             value="${escapeHtml(feed.tag || "")}">
      <button class="row__button" type="button" title="Remove">&times;</button>`;
    const [enabled, tag] = row.querySelectorAll("input");
    enabled.addEventListener("change", (event) => {
      S.feeds[index].enabled = event.target.checked;
    });
    tag.addEventListener("input", (event) => {
      S.feeds[index].tag = event.target.value.trim();
    });
    row.querySelector("button").addEventListener("click", () => {
      S.feeds.splice(index, 1);
      renderFeedRows();
    });
    host.append(row);
  });
  if (!S.feeds.length) host.innerHTML = '<li class="panel__hint">No feeds yet.</li>';
}

/* ------------------------------------------------------------- catalogue */

async function loadCatalog() {
  try {
    const data = await api("/api/catalog");
    S.catalog = data.games || [];
    S.catalogSystem = `${data.system} ${data.arch}`;
    renderCatalogTags(data.tags || []);
    renderCatalog();
  } catch (error) {
    $("catalogList").innerHTML = `<p class="notes">${escapeHtml(error.message)}</p>`;
  }
}

function renderCatalogTags(tags) {
  const host = $("catalogTags");
  host.innerHTML = "";
  ["All", ...tags].forEach((tag) => {
    const chip = document.createElement("button");
    chip.className = `chip${S.catalogTag === tag ? " is-active" : ""}`;
    chip.textContent = tag;
    chip.addEventListener("click", () => { S.catalogTag = tag; renderCatalog(); });
    host.append(chip);
  });
}

function renderCatalog() {
  renderCatalogTags(S.catalog.length ? [...new Set(S.catalog.flatMap((g) => g.tags))].sort() : []);
  const needle = S.catalogFilter.trim().toLowerCase();
  const list = S.catalog.filter((game) => {
    if (S.catalogTag !== "All" && !(game.tags || []).includes(S.catalogTag)) return false;
    if (!needle) return true;
    return `${game.name} ${game.blurb} ${game.licence}`.toLowerCase().includes(needle);
  });

  $("catalogSummary").textContent =
    `${list.length} of ${S.catalog.length} games, all under a free licence with public source. ` +
    `Downloads come from each project's own release page, picked for ${S.catalogSystem || "this computer"}.`;

  const host = $("catalogList");
  host.innerHTML = "";
  list.forEach((game, index) => {
    const card = document.createElement("article");
    card.className = "gamecard";
    card.style.animationDelay = `${Math.min(index, 30) * 12}ms`;
    card.innerHTML = `
      <div class="gamecard__top">
        <p class="gamecard__name">${escapeHtml(game.name)}</p>
        <span class="gamecard__licence">${escapeHtml(game.licence)}</span>
      </div>
      <p class="gamecard__blurb">${escapeHtml(game.blurb)}</p>
      <p class="gamecard__tags">${escapeHtml((game.tags || []).join(" \u00b7 "))}</p>
      ${game.needs_data ? `<p class="gamecard__warn">${escapeHtml(game.needs_data)}</p>` : ""}
      ${game.note ? `<p class="gamecard__warn">${escapeHtml(game.note)}</p>` : ""}
      <div class="gamecard__foot">
        <span class="gamecard__status"></span>
        <a href="${escapeHtml(game.site)}" target="_blank" rel="noopener noreferrer">Site</a>
        <button class="button" type="button">${game.repo ? "Find download" : "Open site"}</button>
      </div>`;
    const button = card.querySelector("button");
    const status = card.querySelector(".gamecard__status");
    button.addEventListener("click", () => {
      if (!game.repo) return openLinkSafely(game.site);
      resolveGame(game, button, status);
    });
    host.append(card);
  });
  if (!list.length) host.innerHTML = '<p class="panel__hint">Nothing matches that filter.</p>';
}

function openLinkSafely(url) {
  if (/^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener");
}

async function resolveGame(game, button, status) {
  button.disabled = true;
  status.textContent = "Checking the project's latest release\u2026";
  try {
    const data = await api("/api/catalog/resolve", { name: game.name });
    const release = data.release || {};
    S.releases[game.name] = release;

    if (release.status === "ok" && release.asset) {
      status.textContent =
        `${release.version || "latest"} \u00b7 ${release.asset.name} ` +
        `(${prettySize(release.asset.size)})${release.cached ? " \u00b7 cached" : ""}`;
      button.textContent = "Download";
      button.className = "button button--primary";
      button.disabled = false;
      button.onclick = () => openGrab(release.asset.url);
    } else {
      status.textContent = release.message || "No matching file in the latest release.";
      button.textContent = "Release page";
      button.disabled = false;
      button.onclick = () => openLinkSafely(release.release_page || release.site || game.site);
    }
  } catch (error) {
    status.textContent = error.message;
    button.textContent = "Site";
    button.disabled = false;
    button.onclick = () => openLinkSafely(game.site);
  }
}

/* ---------------------------------------------------------- downloading */

async function openGrab(url) {
  S.grabbed = null;
  $("grabUrl").value = url || "";
  $("grabDone").hidden = true;
  $("grabGo").disabled = false;
  if (!$("grabFolder").value) {
    try {
      const data = await api("/api/download/folder");
      $("grabFolder").value = data.folder;
    } catch (error) { /* the person can type one */ }
  }
  $("grabDialog").showModal();
}

async function runGrab() {
  const url = $("grabUrl").value.trim();
  if (!url) return toast("Paste a link first", "bad");
  $("grabGo").disabled = true;
  try {
    const { job } = await api("/api/download", { url, folder: $("grabFolder").value.trim() });
    const finished = await runJob(job, "Downloading");
    $("jobDialog").close();
    S.grabbed = finished.result || {};
    $("grabResult").textContent =
      `${S.grabbed.name} — ${prettySize(S.grabbed.size)} saved.` +
      (S.grabbed.installer
        ? " Run it and its own window will ask where to install."
        : " This is an archive, not an installer; unpack it yourself.");
    $("grabRun").hidden = !S.grabbed.installer;
    $("grabDone").hidden = false;
    toast(`Saved ${S.grabbed.name}`);
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("grabGo").disabled = false;
  }
}

async function runGrabbedInstaller() {
  if (!S.grabbed) return;
  try {
    const { job } = await api("/api/download/run", { path: S.grabbed.path });
    const finished = await runJob(job, "Installing");
    const result = finished.result || {};
    $("jobDialog").close();
    toast(result.message || "Finished");
    if ((result.installed || []).length) {
      S.programs = [];
      switchTab("uninstall");
    }
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ----------------------------------------------------------- uninstall */

function prettySize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

async function loadPrograms() {
  const host = $("programList");
  host.innerHTML = '<li class="panel__hint">Reading the installed program list\u2026</li>';
  try {
    const data = await api("/api/programs");
    S.programs = data.programs || [];
    if (!data.supported) {
      $("programSummary").textContent =
        "Uninstalling is a Windows feature. On Linux use your package manager.";
      host.innerHTML = "";
      return;
    }
    renderPrograms();
  } catch (error) {
    host.innerHTML = `<li class="notes">${escapeHtml(error.message)}</li>`;
  }
}

function renderPrograms() {
  const host = $("programList");
  const needle = S.programFilter.trim().toLowerCase();
  const list = S.programs.filter((p) =>
    !needle || `${p.name} ${p.publisher}`.toLowerCase().includes(needle));

  host.innerHTML = "";
  if (!list.length) {
    host.innerHTML = '<li class="panel__hint">Nothing matches that filter.</li>';
    return;
  }
  $("programSummary").textContent =
    `${list.length} of ${S.programs.length} programs. Removing one runs its own ` +
    `uninstaller, then offers to clear out what it leaves behind.`;

  list.slice(0, 400).forEach((program) => {
    const row = document.createElement("li");
    row.className = "prog";
    const meta = [program.publisher, program.version, program.installed]
      .filter(Boolean).join("  \u00b7  ");
    row.innerHTML = `
      <div>
        <p class="prog__name">${escapeHtml(program.name)}</p>
        <p class="prog__meta">${escapeHtml(meta || program.location || "")}</p>
      </div>
      <span class="prog__size">${program.size_kb ? prettySize(program.size_kb * 1024) : ""}</span>
      <button class="button button--danger" type="button">Uninstall</button>`;
    row.querySelector("button").addEventListener("click", () => uninstallProgram(program));
    host.append(row);
  });
}

async function uninstallProgram(program) {
  if (!window.confirm(
      `Run the uninstaller for ${program.name}?\n\n` +
      `Its own window will open and ask you where to go from there.`)) return;
  try {
    const { job } = await api("/api/uninstall", {
      program,
      user: $("account").value,
      silent: false,
    });
    const finished = await runJob(job, `Uninstalling ${program.name}`);
    $("jobDialog").close();
    const result = finished.result || {};
    toast(result.message || "Done");
    if (result.uninstalled) {
      S.programs = S.programs.filter((p) => p.id !== program.id);
      renderPrograms();
      openSweep(program, result.leftovers || [], result.shortcut);
    }
  } catch (error) {
    toast(error.message, "bad");
  }
}

function openSweep(program, leftovers, shortcut) {
  S.sweepProgram = { program, shortcut };
  $("sweepTitle").textContent = `${program.name} is gone`;
  const removable = leftovers.filter((l) => l.deletable);
  const total = removable.reduce((sum, l) => sum + l.size, 0);
  $("sweepIntro").textContent = removable.length
    ? `Its uninstaller left ${removable.length} folder${removable.length === 1 ? "" : "s"} ` +
      `behind, about ${prettySize(total)}. These often hold saves and settings, so ` +
      `check before deleting. Nothing is removed unless you tick it.`
    : "Nothing was left behind.";

  const host = $("sweepList");
  host.innerHTML = "";
  leftovers.forEach((item) => {
    const row = document.createElement("li");
    row.className = "row";
    row.innerHTML = `<input type="checkbox" ${item.deletable ? "" : "disabled"}>
      <span class="row__text">
        <span class="sweep__path">${escapeHtml(item.path)}</span>
        ${item.deletable
          ? `<span class="row__sub">${prettySize(item.size)}</span>`
          : `<span class="sweep__why">kept: ${escapeHtml(item.reason)}</span>`}
      </span>`;
    row.querySelector("input").dataset.path = item.path;
    host.append(row);
  });

  if (shortcut) {
    const row = document.createElement("li");
    row.className = "row";
    row.innerHTML = `<input type="checkbox" checked id="sweepShortcut">
      <span class="row__text">
        <span class="row__name">Also remove its Steam shortcut</span>
        <span class="row__sub">${escapeHtml(shortcut.title)}</span>
      </span>`;
    host.append(row);
  }
  if (!host.children.length) host.innerHTML = '<li class="panel__hint">Nothing to clear up.</li>';
  $("sweepDialog").showModal();
}

async function runSweep() {
  const boxes = [...$("sweepList").querySelectorAll("input[data-path]:checked")];
  const paths = boxes.map((b) => b.dataset.path);
  const alsoShortcut = $("sweepShortcut") && $("sweepShortcut").checked;
  try {
    if (paths.length) {
      const result = await api("/api/leftovers/remove", { paths });
      const refused = result.results.filter((r) => !r.removed);
      toast(`${result.removed} folder${result.removed === 1 ? "" : "s"} removed` +
            (refused.length ? `, ${refused.length} kept` : ""));
    }
    if (alsoShortcut && S.sweepProgram.shortcut) {
      const { job } = await api("/api/remove", {
        user: $("account").value,
        short_ids: [S.sweepProgram.shortcut.short_id],
        delete_art: true,
      });
      await runJob(job, "Removing the Steam shortcut", { silent: true });
    }
    $("sweepDialog").close();
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function findFeed() {
  const url = $("newFeedUrl").value.trim();
  const host = $("feedCandidates");
  if (!url) return toast("Paste an address first", "bad");

  $("findFeed").disabled = true;
  host.innerHTML = '<p class="panel__hint">Looking for a feed\u2026</p>';
  try {
    const data = await api("/api/feeds/discover", { url });
    host.innerHTML = "";
    (data.candidates || []).forEach((candidate) => {
      const already = S.feeds.some((f) => f.url === candidate.url);
      const box = document.createElement("div");
      box.className = "candidate";
      box.innerHTML = `
        <p class="candidate__name">${escapeHtml(candidate.title)}</p>
        <p class="candidate__meta">
          ${candidate.count} entries${candidate.newest
            ? ` \u00b7 newest: ${escapeHtml(candidate.newest)}` : ""}
        </p>
        <div class="candidate__row">
          <input class="input" type="text" placeholder="Name" value="${escapeHtml(candidate.title)}">
          <input class="tagfield" type="text" list="tagOptions" placeholder="Tag">
          <button class="button ${already ? "" : "button--primary"}" type="button"
                  ${already ? "disabled" : ""}>${already ? "Already added" : "Add"}</button>
        </div>`;
      const [nameField, tagField] = box.querySelectorAll("input");
      box.querySelector("button").addEventListener("click", (event) => {
        S.feeds.push({
          name: nameField.value.trim() || candidate.title,
          url: candidate.url,
          enabled: true,
          tag: tagField.value.trim(),
        });
        renderFeedRows();
        event.target.disabled = true;
        event.target.textContent = "Added";
        event.target.className = "button";
        toast("Added. Press Save feeds to keep it.");
      });
      host.append(box);
    });
  } catch (error) {
    host.innerHTML = `<p class="notes">${escapeHtml(error.message)}</p>`;
  } finally {
    $("findFeed").disabled = false;
  }
}


/* ------------------------------------------------------- get software */

function humanSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

const GET_EXAMPLES = [
  ["PCSX2", "https://github.com/PCSX2/pcsx2"],
  ["OpenRCT2", "https://github.com/OpenRCT2/OpenRCT2"],
  ["DOSBox", "https://sourceforge.net/projects/dosbox/"],
];

const KIND_FILTERS = [
  ["all", "Everything"],
  ["file", "Direct downloads"],
  ["swarm", "Magnets and torrents"],
];

function humanSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

function looksLikeUrl(text) {
  const value = (text || "").trim();
  if (!value || value.includes(" ")) return false;
  if (/^(https?:\/\/|magnet:)/i.test(value)) return true;
  return /^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(\/|$)/i.test(value);
}

function renderGetExamples() {
  const host = $("getExamples");
  if (!host || host.childElementCount) return;
  GET_EXAMPLES.forEach(([name, url]) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.textContent = name;
    chip.addEventListener("click", () => { $("getUrl").value = url; runGet(); });
    host.append(chip);
  });
}

function showGetPanel(which) {
  $("getEmpty").hidden = which !== "empty";
  $("getSearch").hidden = which !== "search";
  $("getIndex").hidden = which !== "index";
  $("getFiles").hidden = which !== "files";
  if (which !== "search") $("getSearch").innerHTML = "";
}

/* ---- a site full of programs ---- */

function renderIndex() {
  const host = $("indexList");
  const needle = S.indexFilter.trim().toLowerCase();
  const items = S.indexPrograms.filter(
    (p) => !needle || p.name.toLowerCase().includes(needle)
  );
  $("indexSummary").textContent =
    `${S.indexPrograms.length} programs from ${S.indexSite || "this site"}` +
    (needle ? `, ${items.length} matching` : "") +
    (S.indexExhausted ? "" : " \u2014 more pages available");
  $("indexMore").hidden = S.indexExhausted;

  host.innerHTML = "";
  if (!items.length) {
    host.innerHTML = '<li class="panel__hint">Nothing matches that.</li>';
    return;
  }
  items.slice(0, 400).forEach((program) => {
    const row = document.createElement("li");
    row.className = "row";
    row.innerHTML = `<span class="row__text">
        <span class="row__name">${escapeHtml(program.name)}</span>
        <span class="row__sub">${escapeHtml(program.url)}</span>
      </span>`;
    const button = document.createElement("button");
    button.className = "button";
    button.type = "button";
    button.textContent = "Downloads";
    button.addEventListener("click", () => {
      S.cameFromIndex = true;
      $("getUrl").value = program.url;
      resolvePage("");
    });
    row.append(button);
    host.append(row);
  });
  if (items.length > 400) {
    const note = document.createElement("li");
    note.className = "panel__hint";
    note.textContent = `Showing the first 400 of ${items.length}. Use the search box to narrow it.`;
    host.append(note);
  }
}

async function crawlIndex(url, pages) {
  try {
    const { job } = await api("/api/index", { url, pages });
    const finished = await runJob(job, "Collecting programs");
    const result = finished.result || {};
    S.indexPrograms = result.programs || [];
    S.indexSite = result.site || "";
    S.indexExhausted = !!result.exhausted;
    S.indexPages = pages;
    S.indexUrl = url;
    S.indexFilter = "";
    $("indexFilter").value = "";
    showGetPanel("index");
    renderIndex();
    $("jobDialog").close();
    toast(finished.message);
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ---- one program's files ---- */

function matchesKind(candidate) {
  if (S.getKind === "all") return true;
  if (S.getKind === "file") return candidate.kind === "file";
  return candidate.kind === "magnet" || candidate.kind === "torrent";
}

function renderKindChips() {
  const host = $("getKinds");
  host.innerHTML = "";
  const all = (S.resolved && S.resolved.candidates) || [];
  KIND_FILTERS.forEach(([key, label]) => {
    const count = all.filter((c) => {
      if (key === "all") return true;
      if (key === "file") return c.kind === "file";
      return c.kind === "magnet" || c.kind === "torrent";
    }).length;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${S.getKind === key ? " is-active" : ""}`;
    chip.textContent = count ? `${label} (${count})` : label;
    chip.disabled = count === 0 && key !== "all";
    chip.addEventListener("click", () => { S.getKind = key; renderResolved(); });
    host.append(chip);
  });
}

function renderResolved() {
  const host = $("getResult");
  const data = S.resolved;
  host.innerHTML = "";
  if (!data) return;
  $("getBack").hidden = !S.cameFromIndex;
  renderKindChips();

  const needle = S.getFilter.trim().toLowerCase();
  const visible = data.candidates.filter(
    (c) => matchesKind(c) && (!needle || `${c.name} ${c.label}`.toLowerCase().includes(needle))
  );
  const runs = visible.filter((c) => c.usable);
  const rest = visible.filter((c) => !c.usable);
  if (!visible.length) {
    host.innerHTML = '<p class="panel__hint">Nothing matches those filters.</p>';
    return;
  }

  // If a page also links a lot of other pages, it may be a catalogue that
  // was read as a single project. Offer the other reading rather than
  // leaving someone staring at three files out of hundreds.
  if ((data.entry_count || 0) >= 3) {
    const offer = document.createElement("div");
    offer.className = "pick";
    offer.innerHTML = `<div class="pick__head">
        <p class="pick__title">This page also lists ${data.entry_count} other pages</p>
        <p class="pick__meta">if this is a catalogue rather than one program, collect them all</p>
      </div>`;
    const wrap = document.createElement("div");
    wrap.className = "pick__group";
    const button = document.createElement("button");
    button.className = "button button--accent";
    button.type = "button";
    button.textContent = `Browse as a catalogue`;
    button.addEventListener("click", () => crawlIndex(S.getUrlUsed, 25));
    wrap.append(button);
    offer.append(wrap);
    host.append(offer);
  }

  // What the crawl actually saw. Without this a short result is a mystery,
  // and the only thing to do about it is guess again.
  const diag = data.diagnostics || {};
  const shortfall = document.createElement("div");
  shortfall.className = "pick__group";
  const facts = [];
  if (data.pages_read) facts.push(`${data.pages_read} page${data.pages_read === 1 ? "" : "s"} read`);
  if (diag.entry_like) facts.push(`${diag.entry_like} links to other pages`);
  if (diag.file_like) facts.push(`${diag.file_like} file links`);
  if (diag.scripts > 4) facts.push("script-heavy page");
  const shown = data.shown || {};
  if (shown.swarm_total) facts.push(`${shown.swarm_total} magnets and torrents`);
  if (shown.truncated) facts.push("list truncated");
  shortfall.innerHTML = `<p class="pick__label">What was found</p>
    <p class="field__note" style="margin:0 0 8px">${escapeHtml(facts.join(" \u00b7 ") || "nothing notable")}</p>`;

  const deeper = document.createElement("button");
  deeper.className = "button button--accent";
  deeper.type = "button";
  deeper.textContent = "Scan the whole site";
  deeper.title = "Follow every page of this listing until it runs out";
  deeper.addEventListener("click", () => scanWholeSite());

  const asIndex = document.createElement("button");
  asIndex.className = "button";
  asIndex.type = "button";
  asIndex.textContent = "Treat as a catalogue";
  asIndex.title = "Collect every program this site lists, instead of files on this page";
  asIndex.addEventListener("click", () => crawlIndex(S.getUrlUsed, 50));

  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = "8px";
  row.append(deeper, asIndex);
  shortfall.append(row);

  const box = document.createElement("div");
  box.className = "pick";
  const source = { github: "GitHub release", codeberg: "Codeberg release",
                   sourceforge: "SourceForge release", page: "read from the page" }[data.method]
                 || data.method;
  const meta = [source];
  if (data.version) meta.push(data.version);
  if (data.released) meta.push(data.released);
  if (data.checksums.length) meta.push(`${data.checksums.length} checksum(s) published`);
  if (data.cached) meta.push("cached");
  box.innerHTML = `<div class="pick__head">
      <p class="pick__title">${escapeHtml(data.project || "Downloads")}</p>
      <p class="pick__meta">${escapeHtml(meta.join(" \u00b7 "))}</p>
    </div>`;

  // Older builds are only worth offering when there is a real history,
  // and then only here, next to the files they replace.
  if (S.getReleases.length > 1) {
    const wrap = document.createElement("div");
    wrap.className = "pick__group";
    wrap.innerHTML = '<p class="pick__label">Release</p>';
    const select = document.createElement("select");
    select.className = "input input--select";
    S.getReleases.forEach((release) => {
      const option = document.createElement("option");
      option.value = release.tag;
      option.textContent = `${release.tag}${release.prerelease ? " (pre-release)" : ""}` +
        `${release.published ? " \u00b7 " + release.published : ""}`;
      if (data.version === release.tag) option.selected = true;
      select.append(option);
    });
    select.addEventListener("change", (event) => resolvePage(event.target.value));
    wrap.append(select);
    box.append(wrap);
  }

  const group = (title, items) => {
    if (!items.length) return;
    const wrap = document.createElement("div");
    wrap.className = "pick__group";
    wrap.innerHTML = `<p class="pick__label">${title}</p>`;
    items.forEach((c) => {
      const row = document.createElement("div");
      row.className = "pick__row";
      const bits = [];
      if (c.kind !== "file") bits.push(`<span class="tagchip" data-kind="${c.kind}">${c.kind}</span>`);
      if (c.offsite && c.host) bits.push(`<span class="tagchip" data-kind="offsite">${escapeHtml(c.host)}</span>`);
      if (c.mirrors) bits.push(`<span class="tagchip">+${c.mirrors} mirror</span>`);
      row.innerHTML = `<span class="pick__name">
          <b>${escapeHtml(c.name)}</b>
          <span>${humanSize(c.size)}${c.size ? " \u00b7 " : ""}${bits.join(" ")}</span>
        </span>`;
      const button = document.createElement("button");
      button.className = "button" + (c.usable ? " button--primary" : "");
      button.type = "button";
      button.textContent = c.kind === "magnet" ? "Open in torrent app" : "Download";
      button.addEventListener("click", () => grab(c));
      row.append(button);
      wrap.append(row);
    });
    box.append(wrap);
  };
  group("Runs on this computer", runs);
  group(runs.length ? "Other builds" : "Found on the page", rest);
  if (data.method === "page") box.append(shortfall);
  host.append(box);
  host.append(buildDiagnostics(data));
}

// A plain account of what the parser actually saw. When a page comes back
// with less than expected, this is the difference between guessing and
// knowing.
function buildDiagnostics(data) {
  const details = document.createElement("details");
  details.className = "diag";
  const d = data.diagnostics || {};
  const rows = [
    ["Pages read", data.pages_read],
    ["Links on the first page", d.links_total],
    ["Of those, on this site", d.same_host],
    ["That look like files", d.file_like],
    ["That look like entries", d.entry_like],
    ["Kept as entries", d.entries_kept],
    ["Files usable on page one", d.first_page_usable],
    ["Scripts on the page", d.scripts],
    ["Pagination links found", (d.pagination_found || []).length],
  ].filter(([, value]) => value !== undefined);
  const shapes = (d.top_shapes || [])
    .map(([shape, count]) => `${escapeHtml(shape)} \u00d7${count}`).join(", ");
  details.innerHTML =
    `<summary>What it saw on this page</summary>
     <table class="diag__table">${rows
       .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>
     ${shapes ? `<p class="diag__note">Link shapes: ${shapes}</p>` : ""}
     ${(d.pagination_found || []).length
        ? `<p class="diag__note">Next pages: ${(d.pagination_found || []).slice(0, 3)
            .map((u) => escapeHtml(u)).join("<br>")}</p>` : ""}`;
  return details;
}

async function grab(candidate) {
  try {
    if (candidate.kind === "magnet") {
      await api("/api/magnet/open", { url: candidate.url });
      toast("Handed to your torrent client");
      return;
    }
    const { job } = await api("/api/download", { url: candidate.url });
    const finished = await runJob(job, `Downloading ${candidate.name}`);
    const result = finished.result || {};
    let line = `Saved to ${result.path || "your downloads folder"}`;
    const published = (S.resolved && S.resolved.checksums) || [];
    if (result.sha256 && published.length) {
      const ok = published.some((h) => h.toLowerCase() === result.sha256.toLowerCase());
      line += ok ? " \u2014 checksum matches the page"
                 : " \u2014 CHECKSUM DOES NOT MATCH, do not run this";
    }
    $("jobMessage").textContent = line;
    toast(line.includes("NOT MATCH") ? "Checksum mismatch" : "Downloaded",
          line.includes("NOT MATCH") ? "bad" : "good");
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ---- searching for a project by name ---- */

function renderSearchResults(results) {
  const host = $("getSearch");
  host.innerHTML = "";
  if (!results.length) {
    host.innerHTML = '<p class="panel__hint">No projects matched that name.</p>';
    return;
  }
  const box = document.createElement("div");
  box.className = "pick";
  box.innerHTML = '<div class="pick__head"><p class="pick__title">Projects found</p>' +
    '<p class="pick__meta">pick one to see what it offers</p></div>';
  const group = document.createElement("div");
  group.className = "pick__group";
  results.forEach((item) => {
    const row = document.createElement("div");
    row.className = "pick__row";
    const bits = [`${item.stars.toLocaleString()} stars`];
    if (item.language) bits.push(item.language);
    if (item.updated) bits.push(`updated ${item.updated}`);
    if (item.archived) bits.push("archived");
    row.innerHTML = `<span class="pick__name">
        <b>${escapeHtml(item.full_name)}</b>
        <span>${escapeHtml(item.description || "no description")} \u00b7 ${bits.join(" \u00b7 ")}</span>
      </span>`;
    const button = document.createElement("button");
    button.className = "button";
    button.type = "button";
    button.textContent = "Open";
    button.addEventListener("click", () => { $("getUrl").value = item.url; runGet(); });
    row.append(button);
    group.append(row);
  });
  box.append(group);
  host.append(box);
}

async function loadReleases(url) {
  S.getReleases = [];
  try {
    const history = await api("/api/resolve/releases", { url });
    S.getReleases = history.releases || [];
    if (S.getReleases.length > 1) renderResolved();
  } catch (error) {
    S.getReleases = [];
  }
}

async function resolvePage(tag) {
  const url = tag ? S.getUrlUsed : $("getUrl").value.trim();
  if (!url) return toast("Paste an address first", "bad");
  const button = $("getResolve");
  button.disabled = true;
  button.textContent = "Looking\u2026";
  try {
    const data = await api("/api/resolve",
      { url, tag: tag || "", force: S.getForce, pages: S.filesPages });
    S.getForce = false;

    S.resolved = data;
    S.getUrlUsed = url;
    S.getKind = "all";
    S.getFilter = "";
    $("getFilter").value = "";
    $("getNote").hidden = !data.note;
    $("getNote").textContent = data.note || "";
    if (!tag) S.getReleases = [];
    showGetPanel("files");
    renderResolved();
    if (!tag) loadReleases(url);
    if (!data.candidates.length) toast("No downloads found on that page", "bad");
  } catch (error) {
    S.resolved = null;
    showGetPanel("empty");
    $("getNote").hidden = false;
    $("getNote").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Find";
  }
}

async function scanWholeSite() {
  const url = S.getUrlUsed || $("getUrl").value.trim();
  if (!url) return toast("Find a page first", "bad");
  try {
    const { job } = await api("/api/resolve/deep", { url, force: true });
    const finished = await runJob(job, "Reading every page");
    S.resolved = finished.result || S.resolved;
    S.getUrlUsed = url;
    S.getKind = "all";
    S.getFilter = "";
    $("getFilter").value = "";
    showGetPanel("files");
    renderResolved();
    $("jobDialog").close();
    toast(finished.message);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function searchProjects(term) {
  const button = $("getResolve");
  button.disabled = true;
  button.textContent = "Searching\u2026";
  S.resolved = null;
  try {
    const { results } = await api("/api/resolve/search", { term });
    $("getNote").hidden = true;
    showGetPanel("search");
    renderSearchResults(results);
  } catch (error) {
    showGetPanel("empty");
    $("getNote").hidden = false;
    $("getNote").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Find";
  }
}

function runGet() {
  const value = $("getUrl").value.trim();
  if (!value) return toast("Paste an address or type a program name", "bad");
  S.cameFromIndex = false;
  return looksLikeUrl(value) ? resolvePage("") : searchProjects(value);
}

/* ---- watched pages ---- */

function renderWatched() {
  const host = $("watchList");
  host.innerHTML = "";
  const changed = S.watched.filter((w) => w.changed).length;
  $("watchCount").textContent = S.watched.length
    ? `${S.watched.length} watched${changed ? `, ${changed} changed` : ""}`
    : "none yet";
  // Open it by itself when something moved, so a change is not hidden
  // behind a collapsed panel.
  if (changed) $("watchBox").open = true;
  if (!S.watched.length) {
    host.innerHTML = '<li class="panel__hint">Nothing watched yet.</li>';
    return;
  }
  S.watched.forEach((entry, index) => {
    const row = document.createElement("li");
    row.className = "row";
    const state = entry.error ? entry.error
      : entry.changed ? `changed \u2014 now ${entry.best_name || "different files"}`
      : entry.best_name || "no downloads seen yet";
    row.innerHTML = `<input type="checkbox" ${entry.enabled !== false ? "checked" : ""}>
      <span class="row__text">
        <span class="row__name">${escapeHtml(entry.name || entry.url)}</span>
        <span class="row__sub">${escapeHtml(state)}</span>
      </span>
      <button class="row__button" type="button" title="Remove">&times;</button>`;
    row.querySelector("input").addEventListener("change", async (event) => {
      S.watched[index].enabled = event.target.checked;
      await saveSettings({ watched: S.watched });
    });
    row.querySelector("button").addEventListener("click", async () => {
      S.watched.splice(index, 1);
      await saveSettings({ watched: S.watched });
      renderWatched();
    });
    row.addEventListener("click", (event) => {
      if (event.target.tagName === "BUTTON" || event.target.tagName === "INPUT") return;
      $("getUrl").value = entry.url;
      runGet();
    });
    host.append(row);
  });
}

async function addWatch() {
  const url = $("getUrl").value.trim();
  if (!url) return toast("Paste a page address first", "bad");
  if (S.watched.some((w) => w.url === url)) return toast("Already watching that page");
  const name = (S.resolved && S.resolved.project) || url.replace(/^https?:\/\//, "").split("/")[0];
  S.watched.push({ name, url, enabled: true, fingerprint: "", changed: false });
  await saveSettings({ watched: S.watched });
  renderWatched();
  toast(`Watching ${name}`);
}

async function checkWatched() {
  if (!S.watched.length) return toast("Add a page to watch first", "bad");
  try {
    const { job } = await api("/api/watch/check", {});
    const finished = await runJob(job, "Checking watched pages");
    S.watched = (finished.result || {}).watched || S.watched;
    renderWatched();
    $("jobDialog").close();
    toast(finished.message);
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* --------------------------------------------------------------- wire */

function wire() {
  $("scanButton").addEventListener("click", scan);
  $("matchArt").addEventListener("click", matchArt);
  $("applyButton").addEventListener("click", apply);
  $("removeSelected").addEventListener("click", removeManaged);
  $("jobClose").addEventListener("click", () => $("jobDialog").close());

  $("filter").addEventListener("input", (event) => {
    S.filter = event.target.value;
    renderWall();
  });
  $("selectAll").addEventListener("click", toggleSelectAll);

  $("tabLibrary").addEventListener("click", () => switchTab("library"));
  $("tabManaged").addEventListener("click", () => switchTab("managed"));
  $("tabFeeds").addEventListener("click", () => switchTab("feeds"));
  $("tabGet").addEventListener("click", () => switchTab("get"));
  $("getResolve").addEventListener("click", runGet);
  $("getRefresh").addEventListener("click", () => {
    if (!$("getUrl").value.trim()) return toast("Find something first", "bad");
    S.getForce = true;
    runGet();
  });
  $("getWatch").addEventListener("click", addWatch);
  $("getFilter").addEventListener("input", (event) => {
    S.getFilter = event.target.value;
    renderResolved();
  });
  $("getBack").addEventListener("click", () => {
    S.cameFromIndex = false;
    showGetPanel("index");
    renderIndex();
  });
  $("indexFilter").addEventListener("input", (event) => {
    S.indexFilter = event.target.value;
    renderIndex();
  });
  $("indexClose").addEventListener("click", () => {
    // Catalogue mode used to be a one-way door: no button returned you to
    // the downloads you were looking at.
    S.indexPrograms = [];
    S.cameFromIndex = false;
    if (S.resolved) {
      showGetPanel("files");
      renderResolved();
    } else {
      showGetPanel("empty");
    }
  });
  $("indexMore").addEventListener("click", () => {
    if (!S.indexUrl) return;
    crawlIndex(S.indexUrl, Math.min(S.indexPages * 3, 200));
  });
  $("watchCheck").addEventListener("click", checkWatched);
  $("getUrl").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); runGet(); }
  });
  $("tabUninstall").addEventListener("click", () => switchTab("uninstall"));
  $("tabCatalog").addEventListener("click", () => switchTab("catalog"));
  $("catalogFilter").addEventListener("input", (event) => {
    S.catalogFilter = event.target.value;
    renderCatalog();
  });
  $("reloadPrograms").addEventListener("click", loadPrograms);
  $("programFilter").addEventListener("input", (event) => {
    S.programFilter = event.target.value;
    renderPrograms();
  });
  $("sweepGo").addEventListener("click", runSweep);
  $("linkDownload").addEventListener("click", () => openGrab(""));
  $("grabGo").addEventListener("click", runGrab);
  $("grabRun").addEventListener("click", runGrabbedInstaller);
  $("grabReveal").addEventListener("click", async () => {
    const result = await api("/api/download/reveal", { path: S.grabbed.path });
    if (!result.ok) toast(result.message, "bad");
  });
  $("grabPick").addEventListener("click", async () => {
    const folder = await openBrowse("Where should downloads go?", "dir", $("grabFolder").value);
    if (folder) $("grabFolder").value = folder;
  });
  $("sweepSkip").addEventListener("click", () => $("sweepDialog").close());
  $("refreshFeeds").addEventListener("click", () => loadFeeds(true));
  $("feedFilter").addEventListener("input", (event) => {
    S.feedFilter = event.target.value;
    renderFeeds();
  });
  $("manageFeeds").addEventListener("click", () => { renderFeedRows(); $("feedsDialog").showModal(); });
  $("findFeed").addEventListener("click", findFeed);
  $("newFeedUrl").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); findFeed(); }
  });
  $("saveFeeds").addEventListener("click", async () => {
    $("feedCandidates").innerHTML = "";
    $("newFeedUrl").value = "";
    const result = await api("/api/feeds/save", { feeds: S.feeds });
    S.feeds = result.feeds;
    $("feedsDialog").close();
    toast("Feeds saved");
    loadFeeds(true);
  });
  $("restoreFeeds").addEventListener("click", async () => {
    const result = await api("/api/feeds/save", { restore: true });
    S.feeds = result.feeds;
    renderFeedRows();
    toast("Defaults restored");
  });
  $("account").addEventListener("change", () => { if (S.tab === "managed") loadManaged(); });

  $("openSettings").addEventListener("click", () => $("settingsDialog").showModal());
  $("saveSettings").addEventListener("click", async () => {
    await saveSettings({
      api_key: $("apiKey").value.trim(),
      allow_animated: $("allowAnimated").checked,
      allow_nsfw: $("allowNsfw").checked,
      add_tag: $("addTag").checked,
      art_kinds: [...document.querySelectorAll("#artKindChecks input:checked")].map((i) => i.value),
      github_token: $("githubToken").value.trim(),
    });
    $("settingsDialog").close();
    toast("Settings saved");
  });

  $("overwriteArt").addEventListener("change", (e) => saveSettings({ overwrite_art: e.target.checked }));
  $("preferDirect").addEventListener("change", (e) => saveSettings({ prefer_direct: e.target.checked }));

  $("addFolder").addEventListener("click", async () => {
    const path = await openBrowse("Choose a games folder", "dir", "");
    if (!path) return;
    const folders = [...(S.settings.folders || []), { path, depth: 1 }];
    await saveSettings({ folders });
    renderFolders();
  });

  $("incomingPick").addEventListener("click", async () => {
    const folder = await openBrowse("Where your torrent client saves", "dir",
                                    S.settings.incoming_folder || "");
    if (folder) loadIncoming(folder);
  });
  $("incomingStage").addEventListener("click", stageIncoming);
  $("incomingWatch").addEventListener("change", (event) => {
    if (event.target.checked) startWatching();
    else stopWatching();
  });
  $("incomingInstalled").addEventListener("click", collectInstalled);
  $("addParser").addEventListener("click", () => openParser(null));
  $("pPreset").addEventListener("change", applyPreset);
  $("pSave").addEventListener("click", saveParser);
  $("pDelete").addEventListener("click", async () => {
    const parsers = (S.settings.parsers || []).filter((p) => p.id !== S.editingParser.id);
    await saveSettings({ parsers });
    renderParsers();
    $("parserDialog").close();
  });
  $("pDirPick").addEventListener("click", async () => {
    const path = await openBrowse("Choose the ROM folder", "dir", $("pDir").value);
    if (path) $("pDir").value = path;
  });
  $("pEmuPick").addEventListener("click", async () => {
    const path = await openBrowse("Choose the emulator", "file", $("pEmu").value);
    if (path) { $("pEmu").value = path; applyPreset(); }
  });

  $("browsePick").addEventListener("click", () => finishBrowse(S.browsePath));
  $("browseDialog").addEventListener("close", () => {
    if (S.browseResolve) { S.browseResolve(""); S.browseResolve = null; }
  });

  $("artSearchGo").addEventListener("click", searchGames);
  $("artSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); searchGames(); }
  });
  $("artDialog").addEventListener("close", renderWall);
}

function switchTab(tab) {
  S.tab = tab;
  [["library", "tabLibrary", "libraryView"],
   ["managed", "tabManaged", "managedView"],
   ["feeds", "tabFeeds", "feedsView"],
   ["catalog", "tabCatalog", "catalogView"],
   ["uninstall", "tabUninstall", "uninstallView"],
   ["get", "tabGet", "getView"]].forEach(([name, button, view]) => {
    $(button).classList.toggle("is-active", tab === name);
    $(view).hidden = tab !== name;
  });
  $("sidebar").hidden = tab !== "library";
  document.querySelector(".shell").classList.toggle("is-full", tab !== "library");
  if (tab === "managed") loadManaged();
  if (tab === "feeds" && !S.feedItems.length) loadFeeds(false);
  if (tab === "get") {
    S.watched = S.settings.watched || [];
    renderGetExamples();
    renderWatched();
  }
  if (tab === "uninstall" && !S.programs.length) loadPrograms();
  if (tab === "catalog" && !S.catalog.length) loadCatalog();
  updateDock();
}

boot().then(wire).catch((error) => {
  document.body.innerHTML =
    `<p style="padding:40px;font-family:system-ui">Could not start: ${escapeHtml(error.message)}</p>`;
});
