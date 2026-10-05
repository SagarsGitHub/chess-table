"use strict";

const PIECE_GLYPHS = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const PIECE_NAMES = { k: "king", q: "queen", r: "rook", b: "bishop", n: "knight", p: "pawn" };
const PROMOTION_NAMES = { q: "Queen", r: "Rook", b: "Bishop", n: "Knight" };
const FILES = "abcdefgh";
const POLL_INTERVAL_MS = 1200;

const els = Object.fromEntries([
  "board", "board-help", "board-view-description", "view-physical", "view-legal",
  "turn-indicator", "headline-state", "notice", "connection",
  "hardware-mode", "stop-button", "flip-button", "phase-tag", "session-status",
  "session-start", "session-live", "mode-select", "start-button", "active-mode",
  "move-count", "reset-button", "connection-detail", "parking", "parking-count",
  "transfer-status", "transfer-progress", "transfer-action", "transfer-route",
  "history", "history-count", "recovery-panel", "recovery-detail", "recovery-action",
  "inspected", "promotion-dialog", "promotion-options", "promotion-cancel", "setup-summary",
].map(id => [id, document.getElementById(id)]));

let state = null;
let config = null;
let selected = null;
let dragOrigin = null;
let whiteAtBottom = true;
let viewOverride = null;
let commandInFlight = false;
let online = false;
let localError = "";
let previousBoardSignature = "";
let previousRecoverySignature = "";
let previousHistorySignature = "";
let promotionMoves = [];
let pollInFlight = false;

function prettyError(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    return String(value.message || value.error || value.detail || JSON.stringify(value));
  }
  return "Request failed";
}

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

async function refreshState() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const next = await api("/api/state");
    if (!next || typeof next.board_fen !== "string") throw new Error("Host returned no board position");
    if (Boolean(state?.pending || state?.reset) !== Boolean(next.pending || next.reset)) viewOverride = null;
    if (state?.board_fen !== next.board_fen) selected = null;
    state = next;
    online = true;
    if (localError.startsWith("Connection lost:")) localError = "";
    render();
  } catch (error) {
    online = false;
    localError = `Connection lost: ${prettyError(error)}`;
    render();
    if (!state) {
      els["headline-state"].textContent = "Host unavailable";
      els["session-status"].textContent = "Could not load the saved game.";
    }
  } finally {
    pollInFlight = false;
  }
}

async function command(path, body) {
  if (commandInFlight || !online) return;
  commandInFlight = true;
  localError = "";
  render();
  try {
    const result = await api(path, body);
    if (result && typeof result.board_fen === "string") state = result;
    await refreshState();
  } catch (error) {
    localError = prettyError(error);
    await refreshState();
  } finally {
    commandInFlight = false;
    render();
  }
}

function parseFen(fen) {
  const map = {};
  const placement = String(fen || "").split(" ")[0];
  const ranks = placement.split("/");
  if (ranks.length !== 8) return map;
  for (let row = 0; row < 8; row++) {
    let file = 0;
    for (const char of ranks[row]) {
      if (/[1-8]/.test(char)) file += Number(char);
      else if (PIECE_GLYPHS[char.toLowerCase()] && file < 8) {
        map[`${FILES[file]}${8 - row}`] = char;
        file += 1;
      }
    }
  }
  return map;
}

function legalMoves() {
  return Array.isArray(state?.legal_moves) ? state.legal_moves.filter(move => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)) : [];
}

function canMove() {
  if (!state || !online || commandInFlight || needsReconciliation()) return false;
  if (typeof state.can_move === "boolean") return state.can_move;
  return state.phase === "human_turn" || state.phase === "manual_turn";
}

function activeOperation() { return state?.reset || state?.pending || null; }
function physicalView() { return viewOverride === null ? Boolean(activeOperation()) : viewOverride; }

function needsReconciliation() {
  if (!state) return false;
  if (state.busy) return false;
  const operation = activeOperation();
  if (operation && ["in_flight", "awaiting_confirmation"].includes(operation.phase)) return true;
  return /reconcil|uncertain/i.test(String(state.phase || ""));
}

function render() {
  renderConnection();
  renderNotice();
  if (!state) return;
  renderBoardView();
  renderBoard();
  renderStatus();
  renderTransfer();
  renderSession();
  renderParking();
  renderHistory();
  renderRecovery();
}

function renderConnection() {
  els.connection.classList.toggle("is-online", online);
  els.connection.classList.toggle("is-offline", !online);
  els.connection.textContent = "";
  const dot = document.createElement("span");
  dot.className = "connection-dot";
  els.connection.append(dot, document.createTextNode(online ? " Host connected" : " Host disconnected"));
  if (state) els["hardware-mode"].textContent = state.simulated ? "SIMULATION" : "PHYSICAL BOARD";
  els["stop-button"].textContent = state?.simulated ? "Pause game" : "Stop motion";
  els["stop-button"].title = state?.simulated
    ? "Pause the simulated game after its current transfer."
    : "Stop an active physical carry immediately; an interrupted carry needs inspection and reconciliation.";
  els["stop-button"].classList.toggle("is-simulation", Boolean(state?.simulated));
  els["stop-button"].disabled = !online || !state || !state.can_stop;
}

function renderNotice() {
  const message = localError || prettyError(state?.error || "");
  els.notice.hidden = !message;
  els.notice.textContent = message;
}

function phaseLabel(phase) {
  const labels = {
    idle: "Ready", ready: "Ready", human_turn: "Your move", manual_turn: "Choose a move",
    engine_thinking: "Stockfish thinking", moving: "Board moving", game_over: "Game complete",
    stopped: "Stopped", reconciliation_required: "Inspection needed", resetting: "Resetting board",
    starting: "Preparing board", ending_game: "Ending game", fault: "Inspection needed", error: "Host error",
  };
  return labels[phase] || String(phase || "Ready").replaceAll("_", " ");
}

function gameResult(result) {
  if (result === "1-0") return "White wins · 1–0";
  if (result === "0-1") return "Black wins · 0–1";
  if (result === "1/2-1/2") return "Draw · ½–½";
  return "Game complete";
}

function renderStatus() {
  const phase = state.busy && state.phase === "reconciliation_required" ? "moving" : String(state.phase || "idle");
  const result = state.result;
  const turn = state.turn === "black" || (!state.turn && state.board_fen.split(" ")[1] === "b") ? "Black" : "White";
  let headline = phaseLabel(phase);
  if (phase === "game_over" || (result && result !== "*")) headline = gameResult(result);
  if (needsReconciliation()) headline = "Board inspection needed";
  els["headline-state"].textContent = headline;
  els["headline-state"].classList.toggle("is-alert", needsReconciliation() || Boolean(state.error));
  els["phase-tag"].textContent = phaseLabel(phase);
  els["phase-tag"].classList.toggle("is-alert", needsReconciliation() || Boolean(state.error));
  els["session-status"].textContent = typeof state.status === "string" && state.status ? state.status : headline;
  els["turn-indicator"].replaceChildren();
  const icon = document.createElement("span");
  icon.className = "turn-piece";
  icon.textContent = turn === "White" ? "♙" : "♟";
  const description = document.createElement("span");
  description.textContent = phase === "game_over" ? gameResult(result) : `${turn} to move`;
  els["turn-indicator"].append(icon, description);
  els["board-help"].textContent = needsReconciliation()
    ? "Inspect and reconcile before another move."
    : phase === "game_over" ? "Game complete. Reset the physical board to start another game."
      : canMove() ? (selected ? `Choose a destination for ${selected}.` : "Select a piece, then a highlighted destination.")
      : phase === "ending_game" ? "Finishing the current move before returning pieces to setup."
      : phase === "engine_thinking" ? "Stockfish is choosing a move." : phase === "moving" || state.busy
        ? "The physical move is in progress." : "Moves are available when it is your turn.";
}

function boardSignature() {
  return JSON.stringify([state.board_fen, state.physical_squares, state.moves?.at(-1), state.legal_moves,
    state.can_move, state.busy, state.phase, whiteAtBottom, physicalView(), selected, online, commandInFlight]);
}

function pieceDescription(symbol) {
  return `${symbol === symbol.toUpperCase() ? "white" : "black"} ${PIECE_NAMES[symbol.toLowerCase()] || "piece"}`;
}

function renderBoard() {
  if (dragOrigin) return;
  const signature = boardSignature();
  if (signature === previousBoardSignature) return;
  previousBoardSignature = signature;
  const logical = parseFen(state.board_fen);
  const physical = state.physical_squares || {};
  const isPhysical = physicalView();
  const pieces = isPhysical ? physical : logical;
  const moves = legalMoves();
  const destinations = new Set(selected ? moves.filter(move => move.slice(0, 2) === selected).map(move => move.slice(2, 4)) : []);
  const lastMove = Array.isArray(state.moves) && state.moves.length ? state.moves.at(-1) : "";
  const files = whiteAtBottom ? [...FILES] : [...FILES].reverse();
  const ranks = whiteAtBottom ? [8, 7, 6, 5, 4, 3, 2, 1] : [1, 2, 3, 4, 5, 6, 7, 8];
  const fragment = document.createDocumentFragment();
  els.board.setAttribute("aria-label", `${isPhysical ? "Expected physical board" : "Legal chess position"}, ${whiteAtBottom ? "white" : "black"} at bottom`);
  for (let ri = 0; ri < 8; ri++) {
    for (let fi = 0; fi < 8; fi++) {
      const square = `${files[fi]}${ranks[ri]}`;
      const symbol = pieces[square];
      const button = document.createElement("button");
      button.type = "button";
      button.className = `square ${((FILES.indexOf(files[fi]) + ranks[ri]) % 2) ? "dark" : "light"}`;
      button.dataset.square = square;
      button.setAttribute("aria-label", `${square}${symbol ? `, ${pieceDescription(symbol)}` : ", empty"}${destinations.has(square) ? ", legal destination" : ""}`);
      button.setAttribute("aria-selected", square === selected ? "true" : "false");
      button.disabled = !canMove();
      if (square === selected) button.classList.add("is-selected");
      if (destinations.has(square)) button.classList.add("is-legal");
      if (lastMove && (lastMove.slice(0, 2) === square || lastMove.slice(2, 4) === square)) button.classList.add("is-last");
      if (symbol) {
        button.classList.add("has-piece");
        const piece = document.createElement("span");
        piece.className = `piece ${symbol === symbol.toUpperCase() ? "white" : "black"}`;
        piece.setAttribute("aria-hidden", "true");
        piece.textContent = PIECE_GLYPHS[symbol.toLowerCase()];
        button.append(piece);
        button.draggable = canMove() && moves.some(move => move.slice(0, 2) === square);
        const physicalPawnStandIn = physical[square]?.toLowerCase() === "p" && logical[square]
          && logical[square].toLowerCase() !== "p" &&
          (physical[square] === physical[square].toUpperCase()) === (logical[square] === logical[square].toUpperCase());
        if (physicalPawnStandIn && (isPhysical || !activeOperation())) {
          const standIn = document.createElement("span");
          standIn.className = "stand-in";
          standIn.title = `Physical ${pieceDescription(physical[square])} stands in for ${pieceDescription(logical[square])}`;
          button.append(standIn);
          button.setAttribute("aria-label", `${square}, ${pieceDescription(symbol)}; physical pawn stands in for logical ${pieceDescription(logical[square])}`);
        }
      }
      if (fi === 0) {
        const rankLabel = document.createElement("span");
        rankLabel.className = "square-coordinate square-rank";
        rankLabel.textContent = String(ranks[ri]);
        button.append(rankLabel);
      }
      if (ri === 7) {
        const fileLabel = document.createElement("span");
        fileLabel.className = "square-coordinate";
        fileLabel.textContent = files[fi];
        button.append(fileLabel);
      }
      fragment.append(button);
    }
  }
  els.board.replaceChildren(fragment);
}

function renderBoardView() {
  const physical = physicalView();
  els["view-physical"].setAttribute("aria-pressed", String(physical));
  els["view-legal"].setAttribute("aria-pressed", String(!physical));
  els["board-view-description"].textContent = physical
    ? activeOperation() ? "Expected physical layout at the last confirmed checkpoint" : "Expected physical layout · includes pawn stand-ins"
    : "Committed legal chess position · promoted roles shown";
}

function modeLabel(mode, human) {
  if (mode === "self_play") return "Stockfish vs Stockfish";
  if (mode === "manual_both") return "Manual · both sides";
  if (mode === "human_white" || (mode === "human_engine" && human === "white")) return "You · White";
  if (mode === "human_black" || (mode === "human_engine" && human === "black")) return "You · Black";
  return mode ? String(mode).replaceAll("_", " ") : "No active game";
}

function renderSession() {
  const canStart = typeof state.can_start === "boolean" ? state.can_start : !state.mode;
  els["session-start"].hidden = !canStart;
  els["session-live"].hidden = !state.mode && !(state.moves?.length || state.pending || state.reset);
  els["start-button"].disabled = !online || commandInFlight || !canStart || state.busy || needsReconciliation();
  els["active-mode"].textContent = modeLabel(state.mode, state.human);
  els["move-count"].textContent = `${Array.isArray(state.moves) ? state.moves.length : 0} plies`;
  els["reset-button"].textContent = state.reset ? "Continue board reset"
    : state.reset_requested ? "Finishing move, then resetting…"
      : state.busy && state.mode ? "End game & reset board" : "Reset board for new game";
  const canReset = state.reset ? state.can_reset : state.can_end_and_reset;
  els["reset-button"].disabled = !online || commandInFlight || !canReset;
  const details = [];
  if (state.simulated) details.push("Simulation · no motors move");
  else details.push("Physical controller");
  if (config?.serial_port) details.push(String(config.serial_port));
  if (config?.state_path) details.push(`Checkpoint: ${config.state_path}`);
  els["connection-detail"].textContent = details.join("  ·  ");
}

function renderTransfer() {
  const operation = activeOperation();
  els["transfer-status"].hidden = !operation;
  if (!operation) return;
  const current = Number.isInteger(operation.next_action) ? operation.next_action + 1 : operation.action_number || "?";
  els["transfer-progress"].textContent = `${state.reset ? "Reset" : "Move"} · ${current} / ${operation.total_actions || "?"}`;
  const action = operation.action;
  if (action?.type === "transfer") {
    els["transfer-action"].textContent = `${pieceDescription(action.piece)} · ${locationLabel(action.source)} → ${locationLabel(action.destination)}`;
  } else if (action?.type === "manual_replacement") {
    els["transfer-action"].textContent = `Promotion at ${action.square}`;
  } else {
    els["transfer-action"].textContent = "Preparing the next transfer";
  }
  const route = Array.isArray(operation.active_route) ? operation.active_route : [];
  els["transfer-route"].hidden = route.length === 0;
  els["transfer-route"].textContent = route.length ? `CARRY route: ${route.join(" ")}` : "";
}

function renderSetup() {
  if (!config) return;
  const values = [
    ["Checkpoint", config.state_path],
    ["Connection", config.simulate ? "Simulation" : (config.serial_port || "Physical port not set")],
    ["Stockfish", config.stockfish_path],
    ["Geometry", config.simulate ? config.geometry_file : "Live Uno GEOM"],
    ["Serial log", config.serial_log],
  ];
  const fragment = document.createDocumentFragment();
  for (const [label, value] of values) {
    if (!value) continue;
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = String(value);
    fragment.append(term, definition);
  }
  els["setup-summary"].replaceChildren(fragment);
}

function renderParking() {
  const slots = Array.isArray(state.physical_parking) ? state.physical_parking : [];
  let occupied = 0;
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < 32; i++) {
    const symbol = slots[i];
    const cell = document.createElement("div");
    cell.className = `parking-slot${symbol ? " occupied" : ""}`;
    cell.setAttribute("aria-label", `P${String(i).padStart(2, "0")}: ${symbol ? pieceDescription(symbol) : "empty"}`);
    if (symbol) {
      occupied += 1;
      const piece = document.createElement("b");
      piece.className = symbol === symbol.toUpperCase() ? "white" : "black";
      piece.setAttribute("aria-hidden", "true");
      piece.textContent = PIECE_GLYPHS[symbol.toLowerCase()] || "?";
      cell.append(piece);
    }
    const number = document.createElement("small");
    number.textContent = String(i).padStart(2, "0");
    cell.append(number);
    fragment.append(cell);
  }
  els.parking.replaceChildren(fragment);
  els["parking-count"].textContent = `${occupied} / 32`;
}

function renderHistory() {
  const moves = Array.isArray(state.moves) ? state.moves : [];
  const san = Array.isArray(state.moves_san) && state.moves_san.length === moves.length ? state.moves_san : moves;
  const signature = JSON.stringify([moves, san]);
  if (signature === previousHistorySignature) return;
  previousHistorySignature = signature;
  els["history-count"].textContent = `${moves.length} ${moves.length === 1 ? "ply" : "plies"}`;
  if (!moves.length) {
    const empty = document.createElement("p");
    empty.className = "empty-history";
    empty.textContent = "Moves will appear here.";
    els.history.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < moves.length; index += 2) {
    const row = document.createElement("div");
    row.className = "history-row";
    const number = document.createElement("span");
    number.textContent = `${Math.floor(index / 2) + 1}.`;
    row.append(number);
    for (const offset of [0, 1]) {
      const entry = document.createElement("strong");
      if (index + offset < moves.length) {
        entry.textContent = san[index + offset];
        entry.title = moves[index + offset];
        if (index + offset === moves.length - 1) entry.className = "is-last";
      }
      row.append(entry);
    }
    fragment.append(row);
  }
  els.history.replaceChildren(fragment);
  els.history.scrollTop = els.history.scrollHeight;
}

function locationLabel(location) {
  if (location && typeof location.square === "string") return location.square;
  if (location && Number.isInteger(location.slot)) return `P${String(location.slot).padStart(2, "0")}`;
  return "unknown location";
}

function renderRecovery() {
  const needed = needsReconciliation();
  els["recovery-panel"].hidden = !needed;
  if (!needed) return;
  const operation = activeOperation();
  const isReset = Boolean(state.reset);
  const action = operation?.action || operation?.actions?.[operation?.next_action] || null;
  const signature = JSON.stringify([isReset, operation?.uci, operation?.next_action, operation?.phase, action]);
  if (signature !== previousRecoverySignature) {
    previousRecoverySignature = signature;
    els.inspected.checked = false;
  }
  const index = Number.isInteger(operation?.next_action) ? operation.next_action + 1 : "?";
  const total = operation?.total_actions || "?";
  const target = isReset ? "board reset" : operation?.uci ? `move ${operation.uci}` : "physical move";
  els["recovery-detail"].textContent = `The ${target} stopped during transfer ${index} of ${total}. The saved position cannot establish where the piece ended up.`;
  if (action?.type === "transfer") {
    els["recovery-action"].textContent = `${pieceDescription(action.piece)}: ${locationLabel(action.source)} → ${locationLabel(action.destination)}`;
  } else if (action?.type === "manual_replacement") {
    els["recovery-action"].textContent = `Inspect promotion at ${action.square}`;
  } else {
    els["recovery-action"].textContent = "Inspect the last transfer and its parking slots.";
  }
  const disabled = !els.inspected.checked || commandInFlight || !online || state.busy;
  for (const button of els["recovery-panel"].querySelectorAll("[data-outcome]")) button.disabled = disabled;
}

function chooseDestination(origin, destination) {
  if (!canMove()) return;
  const candidates = legalMoves().filter(move => move.slice(0, 2) === origin && move.slice(2, 4) === destination);
  if (!candidates.length) return;
  selected = null;
  if (candidates.length === 1) {
    void command("/api/move", { uci: candidates[0] });
    return;
  }
  promotionMoves = candidates;
  renderPromotionDialog();
}

function renderPromotionDialog() {
  if (!promotionMoves.length) return;
  const fragment = document.createDocumentFragment();
  for (const role of ["q", "r", "b", "n"]) {
    const move = promotionMoves.find(candidate => candidate.endsWith(role));
    if (!move) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "promotion-choice";
    button.dataset.move = move;
    const glyph = document.createElement("b");
    glyph.textContent = PIECE_GLYPHS[role];
    const label = document.createElement("small");
    label.textContent = PROMOTION_NAMES[role];
    button.append(glyph, label);
    fragment.append(button);
  }
  els["promotion-options"].replaceChildren(fragment);
  els["promotion-dialog"].hidden = false;
  els["promotion-options"].querySelector("button")?.focus();
}

function closePromotionDialog() {
  els["promotion-dialog"].hidden = true;
  promotionMoves = [];
  els.board.querySelector("button:not(:disabled)")?.focus();
}

els.board.addEventListener("click", event => {
  const square = event.target.closest("button[data-square]")?.dataset.square;
  if (!square || !canMove()) return;
  const moves = legalMoves();
  if (selected && moves.some(move => move.startsWith(selected + square))) {
    chooseDestination(selected, square);
    return;
  }
  selected = selected === square ? null : moves.some(move => move.startsWith(square)) ? square : null;
  renderBoard();
  renderStatus();
});

els.board.addEventListener("dragstart", event => {
  const square = event.target.closest("button[data-square]")?.dataset.square;
  if (!square || !canMove() || !legalMoves().some(move => move.startsWith(square))) {
    event.preventDefault();
    return;
  }
  dragOrigin = square;
  selected = square;
  event.dataTransfer.setData("text/plain", square);
  event.dataTransfer.effectAllowed = "move";
});
els.board.addEventListener("dragover", event => {
  const target = event.target.closest("button[data-square]");
  if (!target || !dragOrigin || !legalMoves().some(move => move.startsWith(dragOrigin + target.dataset.square))) return;
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
  const origin = dragOrigin;
  dragOrigin = null;
  els.board.querySelectorAll(".is-drop-target").forEach(element => element.classList.remove("is-drop-target"));
  if (origin && target) chooseDestination(origin, target.dataset.square);
  renderBoard();
});
els.board.addEventListener("dragend", () => {
  dragOrigin = null;
  selected = null;
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
  whiteAtBottom = !whiteAtBottom;
  previousBoardSignature = "";
  renderBoard();
});
els["view-physical"].addEventListener("click", () => {
  viewOverride = true;
  renderBoardView();
  renderBoard();
});
els["view-legal"].addEventListener("click", () => {
  viewOverride = false;
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
  void command(state?.reset ? "/api/reset-board" : "/api/end-game-reset", {});
});
els.inspected.addEventListener("change", renderRecovery);
els["recovery-panel"].addEventListener("click", event => {
  const outcome = event.target.closest("button[data-outcome]")?.dataset.outcome;
  if (!outcome || !els.inspected.checked) return;
  els.inspected.checked = false;
  void command("/api/reconcile", { outcome, confirmed: true });
});

async function initialize() {
  try { config = await api("/api/config"); renderSetup(); }
  catch { /* The state request will report connection problems. */ }
  await refreshState();
  window.setInterval(() => { void refreshState(); }, POLL_INTERVAL_MS);
}

void initialize();
