"use strict";

import {
  POLL_INTERVAL_MS, els, ui, prettyError, legalMoves, canMove, chesscomPositions,
} from "./gui_model.js";
import {
  render, renderBoard, renderStatus, renderBoardView, renderParking,
  renderRecovery, renderSetup, renderPromotionDialog, closePromotionDialog,
  renderChesscom,
} from "./gui_view.js";

let pollInFlight = false;
let chesscomPollInFlight = false;
let chesscomMutation = 0;

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; }
  catch { throw new Error(`Unexpected response from the host (${response.status})`); }
  if (!response.ok) throw new Error(prettyError(data.error || data.message || data));
  return data;
}

// Poll reads a checkpoint snapshot; an in-progress POST never blocks the next read.
async function refreshState() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const next = await api("/api/state");
    if (!next || typeof next.board_fen !== "string") throw new Error("Host returned no board position");
    if (Boolean(ui.state?.pending || ui.state?.reset) !== Boolean(next.pending || next.reset)) ui.viewOverride = null;
    if (ui.state?.board_fen !== next.board_fen) {
      ui.selected = null;
      els["chesscom-confirm"].checked = false;
      els["chesscom-daily-confirm"].checked = false;
    }
    ui.state = next;
    ui.online = true;
    if (next.chesscom) applyChesscomSelection(next.chesscom);
    else void refreshChesscomSelection();
    if (ui.localError.startsWith("Connection lost:")) ui.localError = "";
    render();
  } catch (error) {
    ui.online = false;
    ui.localError = `Connection lost: ${prettyError(error)}`;
    render();
    if (!ui.state) {
      els["headline-state"].textContent = "Host unavailable";
      els["session-status"].textContent = "Could not load the saved game.";
    }
  } finally {
    pollInFlight = false;
  }
}

// Commands share one in-flight guard so double clicks cannot queue physical actions.
async function command(path, body) {
  if (ui.commandInFlight || ui.stopInFlight || !ui.online) return;
  ui.commandInFlight = true;
  ui.localError = "";
  render();
  try {
    const result = await api(path, body);
    if (result && typeof result.board_fen === "string") ui.state = result;
    await refreshState();
  } catch (error) {
    ui.localError = prettyError(error);
    await refreshState();
  } finally {
    ui.commandInFlight = false;
    render();
  }
}

// Promotion is the only origin/destination pair with multiple legal UCI moves.
function chooseDestination(origin, destination) {
  if (!canMove()) return;
  const candidates = legalMoves().filter(move => move.slice(0, 2) === origin && move.slice(2, 4) === destination);
  if (!candidates.length) return;
  ui.selected = null;
  if (candidates.length === 1) {
    void command("/api/move", { uci: candidates[0] });
    return;
  }
  ui.promotionMoves = candidates;
  renderPromotionDialog();
}

els.board.addEventListener("click", event => {
  const square = event.target.closest("button[data-square]")?.dataset.square;
  if (!square || !canMove()) return;
  const moves = legalMoves();
  if (ui.selected && moves.some(move => move.startsWith(ui.selected + square))) {
    chooseDestination(ui.selected, square);
    return;
  }
  ui.selected = ui.selected === square ? null : moves.some(move => move.startsWith(square)) ? square : null;
  renderBoard();
  renderStatus();
});

els.board.addEventListener("dragstart", event => {
  const square = event.target.closest("button[data-square]")?.dataset.square;
  if (!square || !canMove() || !legalMoves().some(move => move.startsWith(square))) {
    event.preventDefault();
    return;
  }
  ui.dragOrigin = square;
  ui.selected = square;
  event.dataTransfer.setData("text/plain", square);
  event.dataTransfer.effectAllowed = "move";
});
els.board.addEventListener("dragover", event => {
  const target = event.target.closest("button[data-square]");
  if (!target || !ui.dragOrigin || !legalMoves().some(move => move.startsWith(ui.dragOrigin + target.dataset.square))) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  target.classList.add("is-drop-target");
});
els.board.addEventListener("dragleave", event => {
  event.target.closest("button[data-square]")?.classList.remove("is-drop-target");
});
els.board.addEventListener("drop", event => {
  event.preventDefault();
  const target = event.target.closest("button[data-square]");
  const origin = ui.dragOrigin;
  ui.dragOrigin = null;
  els.board.querySelectorAll(".is-drop-target").forEach(element => element.classList.remove("is-drop-target"));
  if (origin && target) chooseDestination(origin, target.dataset.square);
  renderBoard();
});
els.board.addEventListener("dragend", () => {
  ui.dragOrigin = null;
  ui.selected = null;
  renderBoard();
});

els["promotion-options"].addEventListener("click", event => {
  const move = event.target.closest("button[data-move]")?.dataset.move;
  if (!move) return;
  closePromotionDialog();
  void command("/api/move", { uci: move });
});
els["promotion-cancel"].addEventListener("click", closePromotionDialog);
els["promotion-dialog"].addEventListener("click", event => {
  if (event.target === els["promotion-dialog"]) closePromotionDialog();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !els["promotion-dialog"].hidden) closePromotionDialog();
});

els["flip-button"].addEventListener("click", () => {
  ui.whiteAtBottom = !ui.whiteAtBottom;
  renderBoard();
  renderParking();
});
els["view-physical"].addEventListener("click", () => {
  ui.viewOverride = true;
  renderBoardView();
  renderBoard();
});
els["view-legal"].addEventListener("click", () => {
  ui.viewOverride = false;
  renderBoardView();
  renderBoard();
});
els["start-button"].addEventListener("click", () => {
  const choice = els["mode-select"].value;
  const body = choice === "human_white" ? { mode: "human_engine", human: "white" }
    : choice === "human_black" ? { mode: "human_engine", human: "black" }
      : { mode: choice };
  void command("/api/start", body);
});
// STOP bypasses browsing and command waits, including a slow public API fetch.
async function priorityStop() {
  if (ui.stopInFlight || !ui.online) return;
  ui.stopInFlight = true;
  chesscomMutation += 1;
  ui.localError = "";
  render();
  try {
    const result = await api("/api/stop", {});
    if (typeof result?.board_fen === "string") ui.state = result;
    await refreshState();
    await refreshChesscomSelection(true);
  } catch (error) {
    ui.localError = prettyError(error);
  } finally {
    ui.stopInFlight = false;
    render();
  }
}
els["stop-button"].addEventListener("click", () => { void priorityStop(); });
els["reset-button"].addEventListener("click", () => {
  void command(ui.state?.reset ? "/api/reset-board" : "/api/end-game-reset", {});
});
els.inspected.addEventListener("change", renderRecovery);
els["recovery-panel"].addEventListener("click", event => {
  const outcome = event.target.closest("button[data-outcome]")?.dataset.outcome;
  if (!outcome || !els.inspected.checked) return;
  els.inspected.checked = false;
  void command("/api/reconcile", { outcome, confirmed: true });
});

// Library previews remain independent from the committed physical board state.
function applyChesscomSelection(data) {
  if (!data || !Object.prototype.hasOwnProperty.call(data, "selection")) return;
  const library = ui.chesscom;
  const selection = data.selection;
  const changed = library.selectionToken !== data.selection_token || library.selection?.id !== selection?.id;
  const dailyUpdated = selection?.kind === "daily" && (library.selection?.pgn !== selection.pgn || library.selection?.fen !== selection.fen || library.selection?.positions?.length !== selection.positions?.length);
  library.selection = selection || null;
  library.selectionToken = data.selection_token || null;
  library.selectionKind = selection?.kind || data.kind || library.selectionKind;
  library.replay = data.replay || null;
  library.daily = data.daily || null;
  if (changed || dailyUpdated) {
    const positions = chesscomPositions(selection);
    library.previewPly = library.selectionKind === "daily" ? Math.max(0, positions.length - 1) : 0;
    els["chesscom-confirm"].checked = false;
    els["chesscom-daily-confirm"].checked = false;
  }
}

async function refreshChesscomSelection(force = false) {
  if (chesscomPollInFlight || (!force && (ui.chesscom.loading || ui.commandInFlight || ui.stopInFlight))) return;
  chesscomPollInFlight = true;
  const revision = chesscomMutation;
  try {
    const data = await api("/api/chesscom/selection");
    if (revision === chesscomMutation) applyChesscomSelection(data);
    render();
  } catch { /* An explicit library action reports its own errors. */ }
  finally { chesscomPollInFlight = false; }
}

async function libraryRequest(action) {
  const library = ui.chesscom;
  if (library.loading || ui.commandInFlight || ui.stopInFlight || !ui.online) return;
  library.loading = true;
  library.error = "";
  renderChesscom();
  try { await action(); }
  catch (error) { library.error = prettyError(error); }
  finally { library.loading = false; renderChesscom(); }
}

async function loadChesscomGames(kind, params = {}) {
  const query = new URLSearchParams(params);
  const data = await api(`/api/chesscom/${kind}${query.size ? `?${query}` : ""}`);
  const games = Array.isArray(data.games) ? data.games : [];
  const library = ui.chesscom;
  library.games[kind] = games;
  library.loaded[kind] = true;
  library.contexts[kind] = { ...params };
  delete library.contexts[kind].q;
  library.status = `${games.length} ${kind === "historical" ? "historic" : kind} ${games.length === 1 ? "game" : "games"}${params.username ? ` for ${params.username}` : ""}${params.year ? ` · ${params.year}-${String(params.month).padStart(2, "0")}` : ""}. Select a game to preview it.`;
}

function normalizeArchive(archive) {
  if (archive && typeof archive === "object" && archive.year && archive.month) {
    return { year: Number(archive.year), month: Number(archive.month) };
  }
  const match = String(archive?.url || archive).match(/\/(\d{4})\/(\d{2})\/?$/);
  return match ? { year: Number(match[1]), month: Number(match[2]) } : null;
}

document.querySelector(".chesscom-tabs").addEventListener("click", event => {
  const kind = event.target.closest("[data-chesscom-kind]")?.dataset.chesscomKind;
  if (!kind || ui.chesscom.loading) return;
  ui.chesscom.kind = kind;
  els["chesscom-query"].value = "";
  const label = { daily: "Load daily games for a public account.", finished: "Load archive months, then search a finished game.", historical: "Explore the historic game collection." };
  ui.chesscom.status = label[kind];
  ui.chesscom.error = "";
  renderChesscom();
});

els["chesscom-account-form"].addEventListener("submit", event => {
  event.preventDefault();
  const username = els["chesscom-username"].value.trim();
  if (!username) return;
  void libraryRequest(async () => {
    ui.chesscom.username = username;
    if (ui.chesscom.kind === "daily") {
      await loadChesscomGames("daily", { username });
      return;
    }
    const data = await api(`/api/chesscom/archives?${new URLSearchParams({ username })}`);
    const archives = (Array.isArray(data.archives) ? data.archives : []).map(normalizeArchive)
      .filter(archive => archive && archive.year >= 1900 && archive.month >= 1 && archive.month <= 12)
      .sort((left, right) => right.year - left.year || right.month - left.month);
    ui.chesscom.archives = archives;
    ui.chesscom.archiveUsername = username;
    const options = document.createDocumentFragment();
    if (!archives.length) options.append(new Option("No archive months available", ""));
    for (const archive of archives) {
      const value = `${archive.year}-${String(archive.month).padStart(2, "0")}`;
      options.append(new Option(value, value));
    }
    els["chesscom-month"].replaceChildren(options);
    ui.chesscom.status = `${archives.length} archive months for ${username}. Choose a month and search; an empty search lists all its finished games.`;
  });
});

els["chesscom-search-form"].addEventListener("submit", event => {
  event.preventDefault();
  const kind = ui.chesscom.kind;
  const q = els["chesscom-query"].value.trim();
  void libraryRequest(async () => {
    if (kind === "historical") { await loadChesscomGames("historical", { q }); return; }
    if (kind !== "finished" || !els["chesscom-month"].value) return;
    const [year, month] = els["chesscom-month"].value.split("-");
    await loadChesscomGames("finished", { username: ui.chesscom.archiveUsername, year, month, q });
  });
});
els["chesscom-month"].addEventListener("change", renderChesscom);

els["chesscom-games"].addEventListener("click", event => {
  const id = event.target.closest("[data-chesscom-game]")?.dataset.chesscomGame;
  if (!id) return;
  void libraryRequest(async () => {
    const kind = ui.chesscom.kind;
    chesscomMutation += 1;
    const data = await api("/api/chesscom/select", { kind, id, ...ui.chesscom.contexts[kind] });
    applyChesscomSelection(data);
    ui.chesscom.status = "Game selected. This screen preview does not move the table.";
  });
});

function previewChesscomPosition(ply) {
  const positions = chesscomPositions();
  if (!positions.length) return;
  ui.chesscom.previewPly = Math.min(Math.max(0, ply), positions.length - 1);
  renderChesscom();
}
els["chesscom-prev"].addEventListener("click", () => previewChesscomPosition((ui.chesscom.previewPly || 0) - 1));
els["chesscom-forward"].addEventListener("click", () => previewChesscomPosition((ui.chesscom.previewPly || 0) + 1));
els["chesscom-moves"].addEventListener("click", event => {
  const ply = event.target.closest("[data-chesscom-ply]")?.dataset.chesscomPly;
  if (ply !== undefined) previewChesscomPosition(Number(ply));
});
els["chesscom-confirm"].addEventListener("change", renderChesscom);
els["chesscom-daily-confirm"].addEventListener("change", renderChesscom);
els["chesscom-wait"].addEventListener("input", renderChesscom);

async function replayChesscom(path, body) {
  if (ui.commandInFlight || ui.stopInFlight || ui.chesscom.loading || !ui.online) return;
  ui.commandInFlight = true;
  ui.chesscom.error = "";
  chesscomMutation += 1;
  render();
  try {
    applyChesscomSelection(await api(path, body));
    els["chesscom-confirm"].checked = false;
    els["chesscom-daily-confirm"].checked = false;
    await refreshState();
    await refreshChesscomSelection(true);
  } catch (error) {
    ui.chesscom.error = prettyError(error);
    await refreshState();
    await refreshChesscomSelection(true);
  } finally {
    ui.commandInFlight = false;
    render();
  }
}
els["chesscom-replay-start"].addEventListener("click", () => {
  if (!els["chesscom-confirm"].checked || !ui.chesscom.replay?.can_start) return;
  void replayChesscom("/api/chesscom/replay/start", { confirmed: true, selection_token: ui.chesscom.selectionToken });
});
els["chesscom-replay-next"].addEventListener("click", () => {
  const replay = ui.chesscom.replay;
  if (!replay?.can_next) return;
  void replayChesscom("/api/chesscom/replay/next", {
    selection_token: replay.selection_token,
    host_game_id: replay.host_game_id,
    expected_ply: replay.index,
    expected_fen: replay.expected_fen,
  });
});
els["chesscom-daily-switch"].addEventListener("click", () => {
  if (!els["chesscom-daily-confirm"].checked || !ui.chesscom.daily?.can_switch) return;
  void replayChesscom("/api/chesscom/daily/switch", { confirmed: true, selection_token: ui.chesscom.selectionToken });
});
els["chesscom-daily-check"].addEventListener("click", () => {
  if (!ui.chesscom.daily?.can_check) return;
  void replayChesscom("/api/chesscom/daily/check", {});
});
els["chesscom-replay-autoplay"].addEventListener("click", () => {
  const replay = ui.chesscom.replay;
  if (!replay?.can_next || replay.autoplay || !els["chesscom-wait"].reportValidity()) return;
  void replayChesscom("/api/chesscom/replay/autoplay", {
    selection_token: replay.selection_token,
    host_game_id: replay.host_game_id,
    expected_ply: replay.index,
    expected_fen: replay.expected_fen,
    wait_seconds: Number(els["chesscom-wait"].value),
  });
});
els["chesscom-replay-pause"].addEventListener("click", () => {
  if (!ui.chesscom.replay?.can_pause) return;
  void replayChesscom("/api/chesscom/replay/pause", {});
});

async function initialize() {
  try { ui.config = await api("/api/config"); renderSetup(); }
  catch { /* The state request will report connection problems. */ }
  await refreshState();
  if (ui.online) await libraryRequest(() => loadChesscomGames("historical"));
  window.setInterval(() => { void refreshState(); }, POLL_INTERVAL_MS);
}

void initialize();
