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
  installs: [],
  library: null,
  activitySeq: 0,
  polledOnce: false,
  pollTimer: null,
  busy: false,
  embed: null,
  embedKey: "",
  programSession: "",
  programHidden: false,
  torrents: { items: [], client: "", error: "" },
  installInfo: null,
  folderPath: "",
  folderParent: "",
  folderMark: "",
  openLogs: new Set(),
};

const ART_LABELS = {
  capsule: "Cover 600\u00d7900",
  wide: "Wide capsule",
  hero: "Hero banner",
  logo: "Logo",
  icon: "Icon",
};

/* ----------------------------------------------------------------- api */

// Every API call carries the token the page was served with, which is what
// stops other web pages from driving the app through the browser.
const TOKEN = (document.querySelector('meta[name="nsgm-token"]') || {}).content || "";

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

function toast(message, tone = "good", action = null) {
  const node = $("toast");
  $("toastText").textContent = message;
  const button = $("toastAction");
  button.hidden = !action;
  if (action) {
    button.textContent = action.label;
    button.onclick = () => { node.hidden = true; action.run(); };
  }
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, action ? 9000 : 4200);
}

/* Questions asked inside the app. The browser's confirm() opens a system
   box outside the window, which is exactly the kind of jump out of the app
   this is meant to avoid. Resolves to false, or { option } when confirmed. */
function ask(title, body, { ok = "OK", cancel = "Cancel", danger = false, option = "",
                            alone = false } = {}) {
  return new Promise((resolve) => {
    const dialog = $("askDialog");
    $("askTitle").textContent = title;
    $("askBody").textContent = body;
    $("askYes").textContent = ok;
    $("askYes").className = `button ${danger ? "button--danger" : "button--primary"}`;
    $("askNo").textContent = cancel;
    $("askNo").hidden = alone;
    $("askOptionRow").hidden = !option;
    $("askOptionLabel").textContent = option;
    $("askOption").checked = false;
    let answer = false;
    $("askYes").onclick = () => { answer = { option: $("askOption").checked }; dialog.close(); };
    $("askNo").onclick = () => dialog.close();
    dialog.addEventListener("close", () => resolve(answer), { once: true });
    dialog.showModal();
    $("askYes").focus();
  });
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
      $("jobSteam").hidden = true;
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
    state.textContent = "Steam is open \u00b7 Close";
  } else {
    state.dataset.state = "closed";
    state.textContent = "Steam is closed";
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
  const client = S.settings.torrent_client || {};
  $("torrentKind").value = client.kind || "system";
  $("torrentUrl").value = client.url || "";
  $("torrentUser").value = client.username || "";
  $("torrentPass").value = client.password || "";
  renderTorrentFields();
  const windows = S.boot.platform === "Windows";
  $("adminRow").hidden = !windows;
  $("runAsAdmin").checked = !!S.settings.run_as_admin;
  $("adminNote").hidden = !windows;
  $("adminNote").textContent = S.boot.admin
    ? "Running with administrator rights now."
    : "Most game installers ask for administrator rights. Windows only lets one program " +
      "hold another\u2019s window when both run with the same rights, so without this they " +
      "open in a window of their own. It means one Windows prompt each time the app starts.";
  $("toolsPath").textContent = S.boot.seven_zip
    ? `7-Zip: ${S.boot.seven_zip}`
    : "7-Zip: not found. Zip and tar archives still unpack without it.";
  S.incomingFolder = S.settings.incoming_folder || "";
  if (S.incomingFolder) $("incomingPath").textContent = S.incomingFolder;
  renderWatchToggle();
  const brand = document.querySelector(".rail__name em");
  // Which build is actually running. Without this there is no way to
  // tell a fixed copy from the one it replaced.
  if (brand && S.boot.version) brand.textContent = `GAME MANAGER  v${S.boot.version}`;

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
    const hasLibrary = (S.settings.library_folders || []).length > 0;
    if (["installer", "ready"].includes(item.state) || (item.state === "archive" && hasLibrary)) {
      const button = document.createElement("button");
      button.className = "row__button row__button--go";
      button.type = "button";
      button.textContent = "Install";
      button.title = item.state === "ready"
        ? "Copy it into your library folder, then stage it for Steam"
        : "Install it into your library folder";
      button.addEventListener("click", () => openInstall(item.path));
      row.append(button);
    } else if (item.state === "archive") {
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
    }
    host.append(row);
  });
}

/* Finished downloads are noticed by the app itself, not by this page, so
   it works with no page open and survives a restart. These only switch it
   on and off and keep the label honest. */

function renderWatchToggle() {
  const auto = !!S.settings.auto_install;
  const box = $("incomingWatch");
  box.checked = auto || !!S.settings.watch_incoming;
  box.disabled = auto;
  $("incomingWatchLabel").textContent = auto
    ? "Watching: new downloads install automatically"
    : "Tell me when downloads finish";
}

async function setWatching(on) {
  if (on && !S.incomingFolder) {
    $("incomingWatch").checked = false;
    return toast("Choose your downloads folder first", "bad");
  }
  try {
    const state = await api("/api/incoming/watch", { on });
    S.settings.watch_incoming = state.watch_incoming;
    renderWatchToggle();
    toast(on ? "You will be told when a download finishes" : "Stopped watching");
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
  if (["feeds", "uninstall", "catalog", "get", "installs"].includes(S.tab)) {
    $("dock").hidden = true;
    return;
  }
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
    $("jobSteam").hidden = false;
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
  // "any" picks a file or the folder being shown, for things to install.
  $("browsePick").hidden = mode === "file";
  loadBrowse(startPath || "");
  $("browseDialog").showModal();
  return new Promise((resolve) => { S.browseResolve = resolve; });
}

async function loadBrowse(path) {
  const list = $("browseList");
  list.innerHTML = '<li><button type="button">Loading\u2026</button></li>';
  try {
    const files = { file: "1", any: "all" }[S.browseMode] || "0";
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}&files=${files}`);
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
    const installable = S.grabbed.installer ||
      /\.(zip|7z|rar|tar|gz|tgz|xz|bz2|appimage|dmg)$/i.test(S.grabbed.name || "");
    $("grabResult").textContent = S.grabbed.queued
      ? `${S.grabbed.name} \u2014 ${prettySize(S.grabbed.size)} saved, and installing ` +
        `automatically. Its progress is under Installs.`
      : `${S.grabbed.name} \u2014 ${prettySize(S.grabbed.size)} saved.` +
        (installable ? " Install puts it into your library." : "");
    $("grabRun").hidden = !installable || !!S.grabbed.queued;
    $("grabDone").hidden = false;
    toast(`Saved ${S.grabbed.name}`);
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("grabGo").disabled = false;
  }
}

function installGrabbed() {
  if (!S.grabbed) return;
  $("grabDialog").close();
  openInstall(S.grabbed.path);
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
  const answer = await ask(`Uninstall ${program.name}?`,
    "Its uninstaller opens inside this window and asks its own questions. " +
    "Afterwards you can clear out what it leaves behind.",
    { ok: "Uninstall", danger: true,
      option: program.can_be_quiet ? "Remove it quietly, without its screens" : "" });
  if (!answer) return;
  try {
    const { job } = await api("/api/uninstall", {
      program,
      user: $("account").value,
      silent: !!answer.option,
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
      button.textContent = c.kind === "magnet" || (c.kind === "torrent" && torrentClient())
        ? (torrentClient() ? `Download with ${torrentClient()}` : "Open in torrent app")
        : "Download";
      button.addEventListener("click", () => grab(c));
      row.append(button);
      wrap.append(row);
    });
    box.append(wrap);
  };
  // Directly under the header, not after the list. A page with thousands
  // of magnets put these controls an unreasonable scroll away.
  if (data.method === "page") box.append(shortfall);
  group("Runs on this computer", runs);
  group(runs.length ? "Other builds" : "Found on the page", rest);
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
    if (candidate.kind === "magnet" || (candidate.kind === "torrent" && torrentClient())) {
      const result = await api("/api/torrent/add", { url: candidate.url, name: candidate.name });
      toast(result.message || "Handed to your torrent app");
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

/* ---------------------------------------------------- held windows */

/* Native windows always draw above web content, so the installer's window
   can only be shown while its panel is the topmost thing on screen. Every
   dialog opening or closing is noticed here and the window follows. */
const DIALOG_STACK = [];
const nativeShowModal = HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal = function showModalTracked() {
  nativeShowModal.call(this);
  DIALOG_STACK.push(this);
  syncEmbed();
};
document.addEventListener("close", (event) => {
  const index = DIALOG_STACK.lastIndexOf(event.target);
  if (index >= 0) DIALOG_STACK.splice(index, 1);
  syncEmbed();
}, true);

function topDialog() {
  for (let i = DIALOG_STACK.length - 1; i >= 0; i -= 1) {
    if (DIALOG_STACK[i].open) return DIALOG_STACK[i];
  }
  return null;
}

function syncEmbed() {
  const session = S.embed;
  const dialog = $("programDialog");
  const visible = !!(session && session.available && !session.popped && dialog.open &&
                     topDialog() === dialog && !document.hidden);
  let rect = null;
  if (visible) {
    const box = $("programStage").getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    rect = [box.left, box.top, box.width, box.height].map((v) => Math.round(v * scale));
  }
  const key = JSON.stringify([visible, rect]);
  if (key === S.embedKey) return;
  S.embedKey = key;
  api("/api/embed/place", { visible, rect }).catch(() => { S.embedKey = ""; });
}

const PROGRAM_STATES = {
  starting: "Starting the installer…",
  embedded: "",
  hidden: "",
  floating: "It is open in a window of its own.",
};

function renderProgram() {
  const session = S.embed;
  const dialog = $("programDialog");
  if (!session) {
    S.programSession = "";
    if (dialog.open) dialog.close();
    return;
  }
  if (session.id !== S.programSession) {
    S.programSession = session.id;
    S.programHidden = false;
  }
  // A quiet install gets the panel only when its installer puts a window up:
  // that is a question nobody would otherwise see.
  const wanted = session.kind === "classic" || session.has_window;
  if (wanted && !S.programHidden && !dialog.open) dialog.showModal();

  const task = S.installs.find((t) => t.id === session.task);
  $("programTitle").textContent = session.window || session.title;
  const facts = session.window ? [session.title] : [];
  if (task && task.target_dir) facts.push(task.target_dir);
  $("programSub").textContent = facts.join(" · ");

  const held = ["embedded", "hidden"].includes(session.state);
  $("programPop").hidden = !session.available || (!held && !session.popped);
  $("programPop").textContent = session.popped ? "Bring back" : "Pop out";
  $("programFront").hidden = session.state !== "floating";
  $("programStop").hidden = !task;

  const placeholder = $("programPlaceholder");
  placeholder.innerHTML = "";
  const line = document.createElement("span");
  line.textContent = session.reason || PROGRAM_STATES[session.state] || "";
  placeholder.append(line);
  if (session.state === "floating" && /administrator/i.test(session.reason || "") &&
      S.boot.platform === "Windows" && !S.boot.admin) {
    const button = document.createElement("button");
    button.className = "button button--accent";
    button.type = "button";
    button.textContent = "Restart with administrator rights";
    button.addEventListener("click", restartElevated);
    placeholder.append(button);
  }

  // The slot grows to the installer's own size, so it is never cropped.
  const [width, height] = session.size || [0, 0];
  const scale = window.devicePixelRatio || 1;
  const stage = $("programStage");
  stage.style.minWidth = width ? `${Math.min(Math.ceil(width / scale) + 24, window.innerWidth - 80)}px` : "";
  stage.style.minHeight = height ? `${Math.min(Math.ceil(height / scale) + 24, window.innerHeight - 200)}px` : "";
  syncEmbed();
}

function hideProgram() {
  S.programHidden = true;
  $("programDialog").close();
  renderActivityChip();
  toast("The installer keeps going. Show it again from Installs.", "good",
        { label: "Show", run: () => { S.programHidden = false; renderProgram(); } });
}

async function restartElevated() {
  const answer = await ask("Restart with administrator rights?",
    "Windows will ask for permission. The app then starts again with the same " +
    "rights installers ask for, so their windows can open inside it. This " +
    "is remembered; turn it off again in Settings.", { ok: "Restart" });
  if (!answer) return;
  try {
    await api("/api/restart-elevated", {});
    document.body.innerHTML =
      '<p style="padding:40px;font-family:system-ui;color:#948aa8">Restarting…</p>';
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* -------------------------------------------------------------- activity */

const ACTIVE = ["queued", "checking", "installing"];

async function pollActivity() {
  clearTimeout(S.pollTimer);
  try {
    const data = await api(`/api/activity?since=${S.activitySeq}`);
    const first = !S.polledOnce;
    S.polledOnce = true;
    S.activitySeq = data.seq;
    S.installs = data.tasks || [];
    S.busy = !!data.busy;
    S.embed = data.embed || null;
    S.torrents = data.torrents || { items: [] };
    updateSteamPill(data.steam_running);
    // Events from before this page loaded were already seen, or were not
    // meant for it; only what happens from now on gets a toast.
    if (!first) (data.events || []).forEach(handleEvent);
    renderProgram();
    renderActivityChip();
    if (S.tab === "installs") renderInstalls();
  } catch (error) {
    // The app may be restarting; the next poll will tell.
  }
  const moving = S.busy || S.embed || S.installs.some((t) => ACTIVE.includes(t.status)) ||
    (S.torrents.items || []).some((t) => t.state !== "done" && !t.missing);
  S.pollTimer = setTimeout(pollActivity, moving ? 1500 : 5000);
}

async function handleEvent(event) {
  if (event.kind === "download-finished") {
    if (S.incomingFolder) loadIncoming();
    if (!event.auto && event.installable) {
      toast(event.text, "good", { label: "Install", run: () => openInstall(event.path) });
    } else {
      toast(event.text);
    }
  } else if (event.kind === "install-done") {
    try {
      const data = await api("/api/games", {});
      S.games = data.games;
      (event.games || []).forEach((uid) => S.selected.add(uid));
      renderWall();
    } catch (error) { /* the shelf refreshes on the next scan anyway */ }
    toast(event.text, "good", { label: "Show", run: () => switchTab("library") });
  } else if (event.kind === "install-failed" || event.kind === "install-waiting") {
    toast(event.text, "bad", { label: "Open", run: () => switchTab("installs") });
  }
}

function percent(value) {
  return value >= 0 ? `${Math.round(value * 100)}%` : "";
}

/* What is going on with installs, on the Installs tab itself: how far the
   running one has got, or how many need an answer. A chip of its own in
   the top bar cost room the section tabs need. */
function renderActivityChip() {
  const running = S.installs.find((t) => t.status === "checking" || t.status === "installing");
  const queued = S.installs.filter((t) => t.status === "queued").length;
  const waiting = S.installs.filter((t) => t.status === "waiting").length;
  const hiddenWindow = S.embed && S.programHidden &&
    (S.embed.kind === "classic" || S.embed.has_window);
  const badge = $("installsBadge");
  const tab = $("tabInstalls");
  let text = "";
  let tone = "busy";
  if (waiting || hiddenWindow) {
    text = hiddenWindow ? "!" : String(waiting);
    tone = "attention";
  } else if (running) {
    text = percent(running.progress) || String(1 + queued);
  } else if (queued) {
    text = String(queued);
  }
  badge.hidden = !text;
  badge.textContent = text;
  badge.dataset.tone = tone;
  tab.title = hiddenWindow ? "An installer is waiting behind the scenes. Open Installs and press Show installer."
    : running ? `Installing ${running.name || ""}${queued ? `, ${queued} more queued` : ""}`
    : waiting ? `${waiting} install${waiting === 1 ? " needs" : "s need"} you` : "";
}

/* ----------------------------------------------------------------- steam */

function updateSteamPill(running) {
  const state = $("steamState");
  if (!S.boot.steam_root) return;
  state.dataset.state = running ? "running" : "closed";
  state.textContent = running ? "Steam is open \u00b7 Close" : "Steam is closed";
  state.title = running ? "Steam overwrites its shortcuts when it exits, so it has to be " +
    "closed before games are added. Click to close it." : "";
  S.boot.steam_running = !!running;
}

async function closeSteam() {
  if (!S.boot.steam_running) return;
  const answer = await ask("Close Steam?", "Steam is asked to exit the same way its own " +
    "menu does, so anything it is doing is saved first.", { ok: "Close Steam" });
  if (!answer) return;
  const pill = $("steamState");
  pill.textContent = "Closing Steam…";
  try {
    const result = await api("/api/steam/close", {});
    updateSteamPill(result.running);
    toast(result.message, result.ok ? "good" : "bad");
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* -------------------------------------------------------------- installs */

const KIND_TAG = { ssd: "SSD", hdd: "HDD", unknown: "?" };
const TASK_STATUS = {
  queued: "Waiting its turn", checking: "Getting ready", installing: "Installing",
  waiting: "Needs you", done: "Installed", failed: "Did not install", cancelled: "Cancelled",
};
const ITEM_KIND = {
  installer: "installer", archive: "archive", folder: "folder of files",
  program: "single program", appimage: "AppImage", dmg: "disk image", pkg: "macOS package",
};

async function loadLibrary() {
  try {
    S.library = await api("/api/library");
    S.settings.library_folders = (S.library.folders || []).map((f) => ({
      path: f.path, kind: f.kind_setting, reserve_gb: f.reserve_gb, enabled: f.enabled }));
    S.settings.auto_install = S.library.auto_install;
    S.settings.watch_incoming = S.library.watch_incoming;
    renderLibrary();
    renderWatchToggle();
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function saveLibrary(patch) {
  try {
    S.library = await api("/api/library/save", patch);
    S.settings.library_folders = (S.library.folders || []).map((f) => ({
      path: f.path, kind: f.kind_setting, reserve_gb: f.reserve_gb, enabled: f.enabled }));
    S.settings.auto_install = S.library.auto_install;
    S.settings.watch_incoming = S.library.watch_incoming;
    renderLibrary();
    renderWatchToggle();
    renderIncoming();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function folderList() {
  return (S.library.folders || []).map((f) => ({
    path: f.path, kind: f.kind_setting, reserve_gb: f.reserve_gb, enabled: f.enabled }));
}

function renderLibrary() {
  const lib = S.library;
  if (!lib) return;
  const host = $("libraryList");
  host.innerHTML = "";
  const folders = lib.folders || [];
  $("libraryCount").textContent = folders.length
    ? `${folders.length} folder${folders.length === 1 ? "" : "s"}` : "none yet";
  if (!folders.length) {
    host.innerHTML = '<li class="panel__hint">Add the folders games should go into, ' +
      "most preferred first. A big hard disk first and a fast SSD after it is a good start.</li>";
    $("libraryBox").open = true;
  }
  folders.forEach((folder, index) => {
    const row = document.createElement("li");
    row.className = "library";
    row.dataset.enabled = folder.enabled ? "yes" : "no";
    const used = folder.total ? Math.round((1 - folder.free / folder.total) * 100) : 0;
    const detected = KIND_TAG[folder.detected] === "?" ? "unknown" : KIND_TAG[folder.detected];
    row.innerHTML = `
      <span class="library__rank">${index + 1}</span>
      <div class="library__main">
        <p class="library__path" title="${escapeHtml(folder.path)}">${escapeHtml(folder.path)}</p>
        <p class="library__meta">${folder.connected
          ? `${prettySize(folder.free)} free of ${prettySize(folder.total)}` +
            `${folder.detail ? ` · ${escapeHtml(folder.detail)}` : ""}` +
            `${folder.exists ? "" : " · created on first install"}`
          : "drive not connected"}</p>
        <div class="meter meter--thin"><div class="meter__fill meter__fill--plain" style="width:${used}%"></div></div>
      </div>
      <span class="library__kind" data-kind="${folder.kind}">${KIND_TAG[folder.kind]}</span>
      <select class="input input--select input--small" title="What kind of drive this is">
        <option value="auto">Detect (${escapeHtml(detected)})</option>
        <option value="ssd">SSD</option>
        <option value="hdd">Hard disk</option>
      </select>
      <label class="library__reserve" title="Space this drive always keeps free">
        keep <input class="input input--small" type="number" min="0" step="1"
                    value="${Math.round(folder.reserve_gb)}"> GB free</label>
      <span class="library__moves">
        <input type="checkbox" title="Use this folder" ${folder.enabled ? "checked" : ""}>
        <button class="row__button" type="button" title="Try earlier" ${index === 0 ? "disabled" : ""}>&uarr;</button>
        <button class="row__button" type="button" title="Try later" ${index === folders.length - 1 ? "disabled" : ""}>&darr;</button>
        <button class="row__button" type="button" title="Remove">&times;</button>
      </span>`;
    const select = row.querySelector("select");
    select.value = folder.kind_setting;
    select.addEventListener("change", () => {
      const next = folderList();
      next[index].kind = select.value;
      saveLibrary({ library_folders: next });
    });
    row.querySelector('input[type="number"]').addEventListener("change", (event) => {
      const next = folderList();
      next[index].reserve_gb = Math.max(0, Number(event.target.value) || 0);
      saveLibrary({ library_folders: next });
    });
    row.querySelector('input[type="checkbox"]').addEventListener("change", (event) => {
      const next = folderList();
      next[index].enabled = event.target.checked;
      saveLibrary({ library_folders: next });
    });
    const [up, down, remove] = row.querySelectorAll(".library__moves button");
    up.addEventListener("click", () => {
      const next = folderList();
      [next[index - 1], next[index]] = [next[index], next[index - 1]];
      saveLibrary({ library_folders: next });
    });
    down.addEventListener("click", () => {
      const next = folderList();
      [next[index + 1], next[index]] = [next[index], next[index + 1]];
      saveLibrary({ library_folders: next });
    });
    remove.addEventListener("click", () => {
      const next = folderList();
      next.splice(index, 1);
      saveLibrary({ library_folders: next });
    });
    host.append(row);
  });

  $("autoInstall").checked = !!lib.auto_install;
  $("ssdPolicy").value = lib.ssd_policy;
  $("installMode").value = lib.install_mode;
  $("preferRecommended").checked = !!lib.prefer_ssd_recommended;
  $("deleteAfter").checked = !!lib.delete_after_install;
  $("autoSteam").checked = !!lib.auto_add_to_steam;

  const note = $("autoNote");
  if (!lib.auto_install) {
    note.textContent = "Downloads wait for you to press Install. Switch on automatic " +
      "installing and finished downloads go straight into your library.";
  } else if (!lib.incoming_folder) {
    note.textContent = "Automatic installing is on, but no downloads folder is set. " +
      "Choose one under Finished downloads on the Add games tab.";
  } else if (!folders.length) {
    note.textContent = "Automatic installing is on. Add a library folder below so " +
      "installs have somewhere to go.";
  } else {
    note.textContent = `Watching ${lib.incoming_folder}. Downloads that finish from now ` +
      "on are installed quietly into the first folder below with room.";
  }
  $("backlogOffer").hidden = !lib.backlog;
  $("backlogText").textContent = `${lib.backlog} download${lib.backlog === 1 ? " is" : "s are"} ` +
    "already in your downloads folder and not installed.";
}

function taskFacts(task) {
  const facts = [];
  if (task.label) facts.push(task.label);
  else if (task.kind) facts.push(ITEM_KIND[task.kind] || task.kind);
  if (task.size) facts.push(`about ${prettySize(task.size)}`);
  if (task.ssd === "required") facts.push("needs an SSD");
  else if (task.ssd === "recommended") facts.push("SSD recommended");
  if (task.mode === "quiet" && task.kind === "installer") facts.push("quiet");
  if (task.auto) facts.push("automatic");
  return facts.join(" · ");
}

function taskButton(label, onClick, tone = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button button--small${tone ? ` button--${tone}` : ""}`;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

async function taskCall(path, body) {
  try {
    await api(path, body);
  } catch (error) {
    toast(error.message, "bad");
  }
  pollActivity();
}

function renderTask(task) {
  const row = document.createElement("li");
  row.className = "task";
  row.dataset.status = task.status;
  const status = TASK_STATUS[task.status] || task.status;
  const pct = task.status === "installing" ? percent(task.progress) : "";
  const where = task.target_dir
    ? `${task.target_dir}${task.target_kind ? ` (${KIND_TAG[task.target_kind] === "?"
        ? "drive type unknown" : KIND_TAG[task.target_kind]})` : ""}` : "";
  row.innerHTML = `
    <div class="task__head">
      <p class="task__name">${escapeHtml(task.name || baseName(task.path))}</p>
      <span class="task__status">${escapeHtml(status)}${pct ? ` · ${pct}` : ""}</span>
    </div>
    ${ACTIVE.includes(task.status)
      ? `<div class="meter"><div class="meter__fill${task.progress < 0 ? " is-seeking" : ""}"
           style="width:${task.progress < 0 ? 100 : Math.round(task.progress * 100)}%"></div></div>` : ""}
    <p class="task__message">${escapeHtml(task.message || "")}</p>
    <p class="task__facts">${escapeHtml(taskFacts(task))}</p>
    ${where ? `<p class="task__where">${escapeHtml(where)}</p>` : ""}
    ${task.warning && task.status !== "done" ? `<p class="task__warn">${escapeHtml(task.warning)}</p>` : ""}
    ${task.result && task.result.steam ? `<p class="task__facts">${escapeHtml(task.result.steam)}</p>` : ""}`;

  const actions = document.createElement("div");
  actions.className = "task__actions";
  const id = task.id;
  if (task.status === "queued") {
    actions.append(taskButton("Cancel", () => taskCall("/api/install/cancel", { id })));
  } else if (task.status === "checking" || task.status === "installing") {
    if (S.embed && S.embed.task === id && (S.embed.kind === "classic" || S.embed.has_window)) {
      actions.append(taskButton("Show installer", () => {
        S.programHidden = false;
        renderProgram();
      }, "primary"));
    }
    actions.append(taskButton("Stop", () => taskCall("/api/install/cancel", { id }), "danger"));
  } else if (task.status === "waiting") {
    if (task.action === "sevenzip") {
      actions.append(taskButton("Get 7-Zip", fetchSevenZip, "primary"));
    } else if (task.action === "classic") {
      actions.append(taskButton("Install with its own screens",
        () => taskCall("/api/install/retry", { id, mode: "classic" }), "primary"));
    }
    actions.append(taskButton("Choose a folder…", async () => {
      const root = await openBrowse("Install it into", "dir", task.target_root || "");
      if (root) taskCall("/api/install/retry", { id, target_root: root });
    }, task.action === "folder" ? "primary" : ""));
    actions.append(taskButton("Try again", () => taskCall("/api/install/retry", { id })));
    actions.append(taskButton("Cancel", () => taskCall("/api/install/cancel", { id })));
  } else if (task.status === "done") {
    const games = (task.result && task.result.games) || [];
    if (task.target_dir) {
      actions.append(taskButton("Open folder", () => openFolder(task.target_dir)));
    }
    if (games.length && !(task.result && task.result.added_to_steam)) {
      actions.append(taskButton("On the shelf", () => switchTab("library"), "primary"));
    }
  } else if (task.status === "failed" || task.status === "cancelled") {
    actions.append(taskButton("Retry", () => taskCall("/api/install/retry", { id }), "primary"));
    if (task.kind === "installer" && task.mode === "quiet") {
      actions.append(taskButton("Use its own screens",
        () => taskCall("/api/install/retry", { id, mode: "classic" })));
    }
  }
  if (!ACTIVE.includes(task.status)) {
    actions.append(taskButton("Remove", () => taskCall("/api/install/remove", { id })));
  }
  if ((task.log || []).length || (task.notes || []).length) {
    const open = S.openLogs.has(id);
    actions.append(taskButton(open ? "Hide details" : "Details", () => {
      if (S.openLogs.has(id)) S.openLogs.delete(id); else S.openLogs.add(id);
      renderInstalls();
    }));
    if (open) {
      const log = document.createElement("pre");
      log.className = "job__log";
      log.textContent = [...(task.log || [])].join("\n") ||
        (task.notes || []).join("\n");
      row.append(actions, log);
      return row;
    }
  }
  row.append(actions);
  return row;
}

function renderTorrents() {
  const info = S.torrents || { items: [] };
  const items = info.items || [];
  $("torrentBlock").hidden = !items.length;
  if (!items.length) return;
  $("torrentClient").textContent = info.client ? `in ${info.client}` : "";
  $("torrentError").hidden = !info.error;
  $("torrentError").textContent = info.error || "";
  const host = $("torrentList");
  host.innerHTML = "";
  items.slice().reverse().forEach((item) => {
    const row = document.createElement("li");
    row.className = "task";
    row.dataset.status = item.state === "done" ? "done" : "installing";
    const pct = Math.round((item.progress || 0) * 100);
    const bits = [];
    if (item.missing) bits.push("no longer in the client");
    else {
      if (item.state) bits.push(item.state);
      if (item.speed && item.state === "downloading") bits.push(`${prettySize(item.speed)}/s`);
      if (item.eta && item.eta < 8640000 && item.state === "downloading") {
        bits.push(item.eta > 3600 ? `${Math.round(item.eta / 3600)} h left`
                                  : `${Math.max(1, Math.round(item.eta / 60))} min left`);
      }
      if (item.size) bits.push(prettySize(item.size));
    }
    row.innerHTML = `
      <div class="task__head">
        <p class="task__name">${escapeHtml(item.name || item.hash)}</p>
        <span class="task__status">${item.missing ? "" : `${pct}%`}</span>
      </div>
      ${item.state !== "done" && !item.missing
        ? `<div class="meter"><div class="meter__fill" style="width:${pct}%"></div></div>` : ""}
      <p class="task__facts">${escapeHtml(bits.join(" · "))}${item.error
        ? ` · ${escapeHtml(item.error)}` : ""}</p>`;
    const actions = document.createElement("div");
    actions.className = "task__actions";
    actions.append(taskButton("Remove from list",
      () => taskCall("/api/torrent/forget", { hash: item.hash })));
    row.append(actions);
    host.append(row);
  });
}

function renderInstalls() {
  const current = S.installs.filter((t) => !["done", "failed", "cancelled"].includes(t.status));
  const finished = S.installs.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
  const list = $("taskList");
  list.innerHTML = "";
  if (!current.length) {
    list.innerHTML = '<li class="panel__hint">Nothing installing. Press Install beside a ' +
      "finished download, or use Install a file.</li>";
  }
  current.forEach((task) => list.append(renderTask(task)));
  $("historyBlock").hidden = !finished.length;
  const history = $("historyList");
  history.innerHTML = "";
  finished.slice().reverse().forEach((task) => history.append(renderTask(task)));
  const needsSeven = S.installs.some((t) => t.status === "waiting" && t.action === "sevenzip");
  $("sevenZipOffer").hidden = !(needsSeven && S.boot.can_fetch_7zip && !S.boot.seven_zip);
  renderTorrents();
}

async function fetchSevenZip() {
  try {
    const { job } = await api("/api/tools/7zip", {});
    const finished = await runJob(job, "Getting 7-Zip");
    S.boot.seven_zip = (finished.result || {}).path || "x";
    $("jobDialog").close();
    toast("7-Zip is ready. Waiting installs carry on.");
    pollActivity();
  } catch (error) {
    toast(error.message, "bad");
  }
}

/* ---------------------------------------------------------- install sheet */

async function openInstall(path) {
  S.installInfo = null;
  $("installTitle").textContent = "Install";
  $("installFacts").textContent = "Looking at it…";
  $("installError").hidden = true;
  $("installForm").hidden = true;
  $("installGo").disabled = true;
  $("installGo").hidden = false;
  $("installDialog").showModal();
  try {
    const info = await api("/api/install/analyse", { path });
    S.installInfo = { ...info, path };
    renderInstallSheet();
  } catch (error) {
    $("installFacts").textContent = "";
    $("installError").hidden = false;
    $("installError").textContent = error.message;
    $("installGo").hidden = true;
  }
}

function sheetTarget() {
  return $("installTarget").value;
}

function renderInstallSheet() {
  const info = S.installInfo;
  const item = info.item;
  $("installTitle").textContent = `Install ${info.name}`;
  const facts = [item.label ? `${item.label} installer` : (ITEM_KIND[item.kind] || item.kind)];
  if (info.size) facts.push(`about ${prettySize(info.size)} (${info.size_note})`);
  if (info.ssd === "required") facts.push(`needs an SSD${info.ssd_note ? ` (“${info.ssd_note}”)` : ""}`);
  else if (info.ssd === "recommended") facts.push(`SSD recommended${info.ssd_note ? ` (“${info.ssd_note}”)` : ""}`);
  const line = facts.join(" \u00b7 ");
  $("installFacts").textContent = line.charAt(0).toUpperCase() + line.slice(1);
  $("installName").value = info.name;

  const select = $("installTarget");
  select.innerHTML = "";
  const usable = (info.targets || []).filter((t) => t.enabled && t.connected);
  usable.forEach((target) => {
    const option = document.createElement("option");
    option.value = target.path;
    option.textContent = `${target.path} — ${KIND_TAG[target.kind] === "?" ? "drive type unknown"
      : KIND_TAG[target.kind]}, ${prettySize(target.free)} free`;
    select.append(option);
  });
  const isInstaller = ["installer", "pkg"].includes(item.kind);
  if (isInstaller) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Let the installer ask where";
    select.append(option);
  }
  const other = document.createElement("option");
  other.value = "__browse";
  other.textContent = "Another folder…";
  select.append(other);
  // When no folder passes the rules, the closest thing to them is offered:
  // an SSD for a game that needs one. Choosing it anyway is the person's call.
  const fallback = (info.ssd === "required" && usable.find((t) => t.kind === "ssd")) || usable[0];
  select.value = info.choice.target || (fallback ? fallback.path : (isInstaller ? "" : "__browse"));

  const notes = $("installNotes");
  notes.innerHTML = "";
  const lines = (info.choice.notes || []).filter((n) => !/: chosen/.test(n)).map((text) => [text, ""]);
  if (info.choice.warning) lines.push([info.choice.warning, "warn"]);
  if (!info.choice.target && usable.length) {
    lines.push(["None of your library folders passes these rules. You can still install " +
                "into one; free some space first if it will not fit.", "warn"]);
  }
  lines.forEach(([text, tone]) => {
    const li = document.createElement("li");
    li.textContent = text;
    if (tone) li.className = "is-warn";
    notes.append(li);
  });

  $("installModes").hidden = !isInstaller;
  const classic = document.querySelector('input[name="installMode"][value="classic"]');
  const quiet = document.querySelector('input[name="installMode"][value="quiet"]');
  quiet.disabled = !info.modes.quiet;
  (info.default_mode === "quiet" && info.modes.quiet ? quiet : classic).checked = true;
  $("installClassicLabel").textContent = info.modes.embed
    ? "The installer’s own screens, inside this window"
    : "The installer’s own screens";
  $("installModeNote").textContent = info.modes.quiet_note ||
    (info.modes.embed ? "" : (S.boot.platform === "Windows"
      ? "In a browser tab the installer opens in a window of its own." : ""));

  $("installForm").hidden = false;
  updateInstallWhere();
}

function updateInstallWhere() {
  const info = S.installInfo;
  if (!info) return;
  const root = sheetTarget();
  const quiet = document.querySelector('input[name="installMode"][value="quiet"]');
  const isInstaller = ["installer", "pkg"].includes(info.item.kind);
  if (isInstaller && !root && quiet.checked) {
    document.querySelector('input[name="installMode"][value="classic"]').checked = true;
  }
  quiet.disabled = !info.modes.quiet || (isInstaller && !root);
  const name = ($("installName").value.trim() || info.name).replace(/[<>:"/\\|?*]/g, " ").trim();
  const sep = root.includes("\\") ? "\\" : "/";
  $("installWhere").textContent = root && root !== "__browse"
    ? `${root.replace(/[\\/]+$/, "")}${sep}${name}`
    : (root === "" ? "The installer shows its own folder choice." : "");
  $("installGo").disabled = root === "__browse";
}

async function chooseInstallFolder() {
  const picked = await openBrowse("Install it into", "dir", "");
  const select = $("installTarget");
  if (picked) {
    let option = [...select.options].find((o) => o.value === picked);
    if (!option) {
      option = document.createElement("option");
      option.value = picked;
      option.textContent = picked;
      select.insertBefore(option, select.lastElementChild);
    }
    select.value = picked;
  } else if (select.value === "__browse") {
    select.selectedIndex = 0;
  }
  updateInstallWhere();
}

async function startInstall() {
  const info = S.installInfo;
  if (!info) return;
  const root = sheetTarget();
  if (root === "__browse") return chooseInstallFolder();
  const mode = (document.querySelector('input[name="installMode"]:checked') || {}).value || "quiet";
  $("installGo").disabled = true;
  try {
    await api("/api/install/start", {
      path: info.path,
      name: $("installName").value.trim() || info.name,
      target_root: root,
      installer_asks: root === "",
      mode: ["installer", "pkg"].includes(info.item.kind) ? mode : "quiet",
      ssd: $("installSsd").value,
    });
    $("installDialog").close();
    switchTab("installs");
    pollActivity();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("installGo").disabled = false;
  }
}

/* ------------------------------------------------------------ folder view */

/* Looking inside a folder without leaving for Explorer. Installers and
   archives can be installed straight from here. */
async function openFolder(path, mark = "") {
  S.folderMark = mark;
  await loadFolder(path);
  if (!$("folderDialog").open) $("folderDialog").showModal();
}

async function loadFolder(path) {
  const list = $("folderList");
  list.innerHTML = '<li><button type="button" disabled>Loading…</button></li>';
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}&files=all`);
    S.folderPath = data.path;
    S.folderParent = data.parent;
    $("folderTitle").textContent = baseName(data.path) || data.path;
    $("folderPath").textContent = data.path;
    $("folderUp").disabled = !data.parent;
    list.innerHTML = "";
    data.dirs.forEach((dir) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `\u{1F4C1}  ${dir.name}`;
      button.addEventListener("click", () => loadFolder(dir.path));
      item.append(button);
      list.append(item);
    });
    (data.files || []).forEach((file) => {
      const item = document.createElement("li");
      item.className = "browse__file";
      if (S.folderMark && file.path === S.folderMark) item.classList.add("is-marked");
      item.innerHTML = `<span class="browse__name">${escapeHtml(file.name)}</span>
        <span class="browse__size">${prettySize(file.size)}</span>`;
      if (["installer", "archive"].includes(file.kind)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "row__button row__button--go";
        button.textContent = "Install";
        button.addEventListener("click", () => {
          $("folderDialog").close();
          openInstall(file.path);
        });
        item.append(button);
      }
      list.append(item);
    });
    if (!list.children.length) list.innerHTML = '<li class="panel__hint">This folder is empty.</li>';
    const marked = list.querySelector(".is-marked");
    if (marked) marked.scrollIntoView({ block: "center" });
  } catch (error) {
    list.innerHTML = `<li class="notes">${escapeHtml(error.message)}</li>`;
  }
}

/* ------------------------------------------------------------ torrents */

function torrentClient() {
  const kind = ((S.settings.torrent_client || {}).kind) || "system";
  return { qbittorrent: "qBittorrent", transmission: "Transmission" }[kind] || "";
}

function renderTorrentFields() {
  const kind = $("torrentKind").value;
  $("torrentFields").hidden = kind === "system";
  $("torrentUrl").placeholder = kind === "transmission" ? "http://127.0.0.1:9091" : "http://127.0.0.1:8080";
}

function torrentConfig() {
  return {
    kind: $("torrentKind").value,
    url: $("torrentUrl").value.trim(),
    username: $("torrentUser").value,
    password: $("torrentPass").value,
  };
}

async function testTorrentClient() {
  const result = $("torrentTestResult");
  result.textContent = "Checking…";
  try {
    const answer = await api("/api/torrent/test", { config: torrentConfig() });
    result.textContent = answer.message;
    result.dataset.tone = answer.ok ? "good" : "bad";
  } catch (error) {
    result.textContent = error.message;
    result.dataset.tone = "bad";
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
  $("grabRun").addEventListener("click", installGrabbed);
  $("grabReveal").addEventListener("click", () => {
    if (!S.grabbed) return;
    $("grabDialog").close();
    openFolder(S.grabbed.path, S.grabbed.path);
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
  // Only useful in a browser tab: a window has its own close button, and
  // closing that already ends the process.
  if (!S.boot.windowed) {
    $("quitApp").hidden = false;
    $("quitApp").addEventListener("click", async () => {
      if (!(await ask("Close the app?", "Anything still downloading or installing will stop.",
                      { ok: "Close", danger: true }))) return;
      try { await api("/api/quit", {}); } catch (error) { /* it is going away */ }
      document.body.innerHTML =
        '<p style="padding:40px;font-family:system-ui;color:#948aa8">' +
        'Closed. You can shut this tab.</p>';
    });
  }
  $("saveSettings").addEventListener("click", async () => {
    await saveSettings({
      api_key: $("apiKey").value.trim(),
      allow_animated: $("allowAnimated").checked,
      allow_nsfw: $("allowNsfw").checked,
      add_tag: $("addTag").checked,
      art_kinds: [...document.querySelectorAll("#artKindChecks input:checked")].map((i) => i.value),
      github_token: $("githubToken").value.trim(),
      torrent_client: torrentConfig(),
      run_as_admin: $("runAsAdmin").checked,
    });
    $("settingsDialog").close();
    toast("Settings saved");
    if (S.settings.run_as_admin && S.boot.platform === "Windows" && !S.boot.admin) {
      restartElevated();
    }
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
  $("incomingWatch").addEventListener("change", (event) => setWatching(event.target.checked));

  // installs
  $("tabInstalls").addEventListener("click", () => switchTab("installs"));
  const strip = document.querySelector(".rail__tabs");
  const fitStrip = () => strip.classList.toggle("is-overflowing",
                                                strip.scrollWidth > strip.clientWidth + 1);
  new ResizeObserver(fitStrip).observe(strip);
  strip.addEventListener("wheel", (event) => {
    if (!event.deltaY || strip.scrollWidth <= strip.clientWidth) return;
    strip.scrollLeft += event.deltaY;
    event.preventDefault();
  }, { passive: false });
  $("installFile").addEventListener("click", async () => {
    const start = S.incomingFolder || S.settings.incoming_folder || "";
    const path = await openBrowse("Choose an installer, archive or game folder", "any", start);
    if (path) openInstall(path);
  });
  $("autoInstall").addEventListener("change", async (event) => {
    const on = event.target.checked;
    if (on) {
      const answer = await ask("Install new downloads automatically?",
        "Downloads that finish from now on are installed quietly into your library " +
        "folders, without asking. Installers whose maker is not recognised still wait for " +
        "you. What is already in the folder stays where it is.",
        { ok: "Switch it on" });
      if (!answer) { event.target.checked = false; return; }
    }
    saveLibrary({ auto_install: on });
  });
  $("libraryAdd").addEventListener("click", async () => {
    const path = await openBrowse("Choose a folder to install games into", "dir", "");
    if (!path) return;
    saveLibrary({ library_folders: [...folderList(), { path, kind: "auto", reserve_gb: 10,
                                                       enabled: true }] });
  });
  $("ssdPolicy").addEventListener("change", (e) => saveLibrary({ ssd_policy: e.target.value }));
  $("installMode").addEventListener("change", (e) => saveLibrary({ install_mode: e.target.value }));
  $("preferRecommended").addEventListener("change",
    (e) => saveLibrary({ prefer_ssd_recommended: e.target.checked }));
  $("deleteAfter").addEventListener("change",
    (e) => saveLibrary({ delete_after_install: e.target.checked }));
  $("autoSteam").addEventListener("change",
    (e) => saveLibrary({ auto_add_to_steam: e.target.checked }));
  $("sevenZipGet").addEventListener("click", fetchSevenZip);
  $("backlogGo").addEventListener("click", async () => {
    try {
      const { queued } = await api("/api/install/backlog", {});
      toast(`${queued} queued to install`);
      loadLibrary();
      pollActivity();
    } catch (error) {
      toast(error.message, "bad");
    }
  });
  $("historyClear").addEventListener("click", () => taskCall("/api/install/clear", {}));
  $("installTarget").addEventListener("change", () => {
    if (sheetTarget() === "__browse") chooseInstallFolder();
    else updateInstallWhere();
  });
  $("installName").addEventListener("input", updateInstallWhere);
  document.querySelectorAll('input[name="installMode"]').forEach((radio) =>
    radio.addEventListener("change", updateInstallWhere));
  $("installGo").addEventListener("click", startInstall);

  // the installer's window, held inside the app
  $("programHide").addEventListener("click", hideProgram);
  $("programDialog").addEventListener("cancel", (event) => { event.preventDefault(); hideProgram(); });
  $("programPop").addEventListener("click", async () => {
    const back = S.embed && S.embed.popped;
    try {
      const { embed } = await api(back ? "/api/embed/bringback" : "/api/embed/popout", {});
      S.embed = embed;
      renderProgram();
    } catch (error) {
      toast(error.message, "bad");
    }
  });
  $("programFront").addEventListener("click", () => api("/api/embed/front", {}).catch(() => {}));
  $("programStop").addEventListener("click", async () => {
    const session = S.embed;
    if (!session || !session.task) return;
    const answer = await ask("Stop this install?", "The installer is closed where it stands. " +
      "Most installers clean up after themselves, but some leave a half-installed folder.",
      { ok: "Stop it", danger: true });
    if (answer) taskCall("/api/install/cancel", { id: session.task });
  });
  new ResizeObserver(syncEmbed).observe($("programStage"));
  window.addEventListener("resize", syncEmbed);
  document.addEventListener("visibilitychange", syncEmbed);
  $("programDialog").addEventListener("scroll", () => { S.embedKey = ""; syncEmbed(); });

  // folders, without Explorer
  $("folderUp").addEventListener("click", () => { if (S.folderParent) loadFolder(S.folderParent); });
  $("folderSystem").addEventListener("click", async () => {
    const result = await api("/api/reveal", { path: S.folderMark || S.folderPath });
    if (!result.ok) toast(result.message, "bad");
  });

  // steam
  $("steamState").addEventListener("click", closeSteam);
  $("jobSteam").addEventListener("click", async () => {
    const result = await api("/api/steam/start", {});
    toast(result.message, result.ok ? "good" : "bad");
    $("jobDialog").close();
  });

  // settings: torrent client and administrator rights
  $("torrentKind").addEventListener("change", renderTorrentFields);
  $("torrentTest").addEventListener("click", testTorrentClient);
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
   ["installs", "tabInstalls", "installsView"],
   ["get", "tabGet", "getView"]].forEach(([name, button, view]) => {
    $(button).classList.toggle("is-active", tab === name);
    $(view).hidden = tab !== name;
  });
  const active = document.querySelector(".tab.is-active");
  if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
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
  if (tab === "installs") { loadLibrary(); renderInstalls(); }
  updateDock();
}

async function restoreSession() {
  try {
    const data = await api("/api/games", {});
    if (!(data.games || []).length) return;
    S.games = data.games;
    S.matches = data.matches || {};
    S.selected = new Set(S.games.map((g) => g.uid));
    renderWall();
    const covered = Object.values(S.matches).filter((m) => m && m.thumb).length;
    toast(`Restored ${S.games.length} games${covered ? ` and ${covered} covers` : ""}`);
  } catch (error) {
    // Nothing saved yet is the normal case, not a problem worth reporting.
  }
}

async function checkForUpdate() {
  try {
    const info = await api("/api/update");
    S.update = info;
    if (!info.available) {
      // A check that could not run and a check that found nothing looked
      // identical: no badge either way. Say which it was.
      if (!info.checked || info.note) {
        const badge = $("updateBadge");
        badge.hidden = false;
        badge.textContent = "Update check failed";
        badge.style.borderColor = "var(--dimmer)";
        badge.style.color = "var(--dim)";
        badge.title = info.note || "Could not reach GitHub.";
        badge.addEventListener("click", () => ask(
          `Running version ${info.current || "unknown"}`,
          info.note || "The update check could not complete.", { alone: true }));
      }
      return;
    }
    const badge = $("updateBadge");
    badge.hidden = false;
    badge.textContent = `Update to ${info.version}`;
    badge.title = info.note || `You have ${info.current}. Released ${info.published}.`;
    badge.addEventListener("click", async () => {
      // Where the program can replace its own file, do that. Otherwise
      // fall back to opening the download, which is all that is possible
      // when running from source or from a folder we cannot write to.
      if (!info.can_replace || !info.binary) {
        // Say why before sending someone off to reinstall by hand, rather
        // than silently opening a download and leaving them to work it out.
        const reason = info.why_not ||
          "no in-place download is published for this system";
        if (!(await ask("This copy cannot update itself", `${reason}. Open the download ` +
                        "page in your browser instead?", { ok: "Open page" }))) return;
        window.open(info.download || info.page, "_blank", "noopener");
        return;
      }
      if (!(await ask(`Update to ${info.version}?`,
                      "It is installed now and takes effect when you restart.",
                      { ok: "Update" }))) return;
      badge.disabled = true;
      try {
        const { job } = await api("/api/update/apply", { url: info.binary });
        await runJob(job, `Installing ${info.version}`);
        badge.textContent = "Restart to finish";
        badge.disabled = false;
        badge.title = "The new version is in place and starts with the next launch.";
        badge.onclick = null;
      } catch (error) {
        badge.disabled = false;
        toast(error.message, "bad");
        $("jobDialog").close();
      }
    });
  } catch (error) {
    // A failed update check is never worth interrupting anyone over.
  }
}

boot().then(wire).then(restoreSession).then(() => {
  if (S.incomingFolder) loadIncoming();
  pollActivity();
  return checkForUpdate();
}).catch((error) => {
  document.body.innerHTML =
    `<p style="padding:40px;font-family:system-ui">Could not start: ${escapeHtml(error.message)}</p>`;
});
