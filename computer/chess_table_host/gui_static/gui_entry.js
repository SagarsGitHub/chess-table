"use strict";

import {
  POLL_INTERVAL_MS, els, ui, prettyError, legalMoves, canMove,
} from "./gui_model.js";
import {
  render, renderBoard, renderStatus, renderBoardView, renderParking,
  renderRecovery, renderSetup, renderPromotionDialog, closePromotionDialog,
} from "./gui_view.js";

let pollInFlight = false;

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
    if (ui.state?.board_fen !== next.board_fen) ui.selected = null;
    ui.state = next;
    ui.online = true;
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
  if (ui.commandInFlight || !ui.online) return;
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
els["stop-button"].addEventListener("click", () => { void command("/api/stop", {}); });
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

async function initialize() {
  try { ui.config = await api("/api/config"); renderSetup(); }
  catch { /* The state request will report connection problems. */ }
  await refreshState();
  window.setInterval(() => { void refreshState(); }, POLL_INTERVAL_MS);
}

void initialize();
