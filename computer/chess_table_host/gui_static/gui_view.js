"use strict";

import {
  PIECE_NAMES, PROMOTION_NAMES, FILES, els, ui, prettyError, parseFen,
  legalMoves, canMove, activeOperation, physicalView, needsReconciliation,
} from "./gui_model.js";

// Keep high-frequency polling from rebuilding the board, parking, and history
// unless their inputs change. A drag keeps its DOM node alive until drop.
let previousBoardSignature = "";
let previousRecoverySignature = "";
let previousHistorySignature = "";
let previousParkingSignature = "";

export function render() {
  renderConnection();
  renderNotice();
  if (!ui.state) return;
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
  els.connection.classList.toggle("is-online", ui.online);
  els.connection.classList.toggle("is-offline", !ui.online);
  els.connection.textContent = "";
  const dot = document.createElement("span");
  dot.className = "connection-dot";
  els.connection.append(dot, document.createTextNode(ui.online ? " Host connected" : " Host disconnected"));
  if (ui.state) els["hardware-mode"].textContent = ui.state.simulated ? "SIMULATION" : "PHYSICAL BOARD";
  els["stop-button"].textContent = ui.state?.simulated ? "Pause game" : "Stop motion";
  els["stop-button"].title = ui.state?.simulated
    ? "Pause the simulated game after its current transfer."
    : "Stop an active physical carry immediately; an interrupted carry needs inspection and reconciliation.";
  els["stop-button"].classList.toggle("is-simulation", Boolean(ui.state?.simulated));
  els["stop-button"].disabled = !ui.online || !ui.state || !ui.state.can_stop;
}

function renderNotice() {
  const message = ui.localError || prettyError(ui.state?.error || "");
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

export function renderStatus() {
  const phase = ui.state.busy && ui.state.phase === "reconciliation_required" ? "moving" : String(ui.state.phase || "idle");
  const result = ui.state.result;
  const turn = ui.state.turn === "black" || (!ui.state.turn && ui.state.board_fen.split(" ")[1] === "b") ? "Black" : "White";
  let headline = phaseLabel(phase);
  if (phase === "game_over" || (result && result !== "*")) headline = gameResult(result);
  if (needsReconciliation()) headline = "Board inspection needed";
  els["headline-state"].textContent = headline;
  els["headline-state"].classList.toggle("is-alert", needsReconciliation() || Boolean(ui.state.error));
  els["phase-tag"].textContent = phaseLabel(phase);
  els["phase-tag"].classList.toggle("is-alert", needsReconciliation() || Boolean(ui.state.error));
  els["session-status"].textContent = typeof ui.state.status === "string" && ui.state.status ? ui.state.status : headline;
  els["turn-indicator"].replaceChildren();
  const icon = pieceToken(turn === "White" ? "P" : "p", "turn-piece");
  const description = document.createElement("span");
  description.textContent = phase === "game_over" ? gameResult(result) : `${turn} to move`;
  els["turn-indicator"].append(icon, description);
  els["board-help"].textContent = needsReconciliation()
    ? "Inspect and reconcile before another move."
    : phase === "game_over" ? "Game complete. Reset the physical board to start another game."
      : canMove() ? (ui.selected ? `Choose a destination for ${ui.selected}.` : "Select a piece, then a highlighted destination.")
      : phase === "ending_game" ? "Finishing the current move before returning pieces to setup."
      : phase === "engine_thinking" ? "Stockfish is choosing a move." : phase === "moving" || ui.state.busy
        ? "The physical move is in progress." : "Moves are available when it is your turn.";
}

function boardSignature() {
  return JSON.stringify([ui.state.board_fen, ui.state.physical_squares, ui.state.moves?.at(-1), ui.state.legal_moves,
    ui.state.can_move, ui.state.busy, ui.state.phase, ui.whiteAtBottom, physicalView(), ui.selected, ui.online, ui.commandInFlight]);
}

function pieceDescription(symbol) {
  return `${symbol === symbol.toUpperCase() ? "white" : "black"} ${PIECE_NAMES[symbol.toLowerCase()] || "piece"}`;
}

function pieceIcon(symbol) {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.classList.add("piece-icon");
  icon.setAttribute("viewBox", "0 0 64 64");
  icon.setAttribute("aria-hidden", "true");
  icon.setAttribute("focusable", "false");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#piece-${symbol.toLowerCase()}`);
  icon.append(use);
  return icon;
}

function pieceToken(symbol, extraClass = "") {
  const side = symbol === symbol.toUpperCase() ? "white" : "black";
  const token = document.createElement("span");
  token.className = `piece-token ${side}${extraClass ? ` ${extraClass}` : ""}`;
  token.setAttribute("aria-hidden", "true");
  token.append(pieceIcon(symbol));
  return token;
}

// Promotion changes the legal role, but the same pawn remains on the table.
function physicalPawnStandsInFor(logicalPiece, physicalPiece) {
  return Boolean(physicalPiece && logicalPiece && physicalPiece.toLowerCase() === "p"
    && logicalPiece.toLowerCase() !== "p"
    && (physicalPiece === physicalPiece.toUpperCase()) === (logicalPiece === logicalPiece.toUpperCase()));
}

// Render either committed chess roles or the expected physical checkpoint.
export function renderBoard() {
  if (ui.dragOrigin) return;
  const signature = boardSignature();
  if (signature === previousBoardSignature) return;
  previousBoardSignature = signature;
  const logical = parseFen(ui.state.board_fen);
  const physical = ui.state.physical_squares || {};
  const isPhysical = physicalView();
  const pieces = isPhysical ? physical : logical;
  const moves = legalMoves();
  const destinations = new Set(ui.selected ? moves.filter(move => move.slice(0, 2) === ui.selected).map(move => move.slice(2, 4)) : []);
  const lastMove = Array.isArray(ui.state.moves) && ui.state.moves.length ? ui.state.moves.at(-1) : "";
  const files = ui.whiteAtBottom ? [...FILES] : [...FILES].reverse();
  const ranks = ui.whiteAtBottom ? [8, 7, 6, 5, 4, 3, 2, 1] : [1, 2, 3, 4, 5, 6, 7, 8];
  const fragment = document.createDocumentFragment();
  els.board.setAttribute("aria-label", `${isPhysical ? "Expected physical board" : "Legal chess position"}, ${ui.whiteAtBottom ? "white" : "black"} at bottom`);
  for (let ri = 0; ri < 8; ri++) {
    for (let fi = 0; fi < 8; fi++) {
      const square = `${files[fi]}${ranks[ri]}`;
      const symbol = pieces[square];
      const button = document.createElement("button");
      button.type = "button";
      button.className = `square ${((FILES.indexOf(files[fi]) + ranks[ri]) % 2) ? "dark" : "light"}`;
      button.dataset.square = square;
      button.setAttribute("aria-label", `${square}${symbol ? `, ${pieceDescription(symbol)}` : ", empty"}${destinations.has(square) ? ", legal destination" : ""}`);
      button.setAttribute("aria-selected", square === ui.selected ? "true" : "false");
      button.disabled = !canMove();
      if (square === ui.selected) button.classList.add("is-selected");
      if (destinations.has(square)) button.classList.add("is-legal");
      if (lastMove && (lastMove.slice(0, 2) === square || lastMove.slice(2, 4) === square)) button.classList.add("is-last");
      if (symbol) {
        button.classList.add("has-piece");
        button.append(pieceToken(symbol));
        button.draggable = canMove() && moves.some(move => move.slice(0, 2) === square);
        if (physicalPawnStandsInFor(logical[square], physical[square]) && (isPhysical || !activeOperation())) {
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

export function renderBoardView() {
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
  const canStart = typeof ui.state.can_start === "boolean" ? ui.state.can_start : !ui.state.mode;
  els["session-start"].hidden = !canStart;
  els["session-live"].hidden = !ui.state.mode && !(ui.state.moves?.length || ui.state.pending || ui.state.reset);
  els["start-button"].disabled = !ui.online || ui.commandInFlight || !canStart || ui.state.busy || needsReconciliation();
  els["active-mode"].textContent = modeLabel(ui.state.mode, ui.state.human);
  els["move-count"].textContent = `${Array.isArray(ui.state.moves) ? ui.state.moves.length : 0} plies`;
  els["reset-button"].textContent = ui.state.reset ? "Continue board reset"
    : ui.state.reset_requested ? "Finishing move, then resetting…"
      : ui.state.busy && ui.state.mode ? "End game & reset board" : "Reset board for new game";
  const canReset = ui.state.reset ? ui.state.can_reset : ui.state.can_end_and_reset;
  els["reset-button"].disabled = !ui.online || ui.commandInFlight || !canReset;
  const details = [];
  if (ui.state.simulated) details.push("Simulation · no motors move");
  else details.push("Physical controller");
  if (ui.config?.serial_port) details.push(String(ui.config.serial_port));
  if (ui.config?.state_path) details.push(`Checkpoint: ${ui.config.state_path}`);
  els["connection-detail"].textContent = details.join("  ·  ");
}

function renderTransfer() {
  const operation = activeOperation();
  els["transfer-status"].hidden = !operation;
  if (!operation) return;
  const current = Number.isInteger(operation.next_action) ? operation.next_action + 1 : operation.action_number || "?";
  const clearanceNote = operation.castle_clearance_override ? " · relaxed clearance" : "";
  els["transfer-progress"].textContent = `${ui.state.reset ? "Reset" : "Move"} · ${current} / ${operation.total_actions || "?"}${clearanceNote}`;
  const action = operation.action;
  if (action?.type === "transfer") {
    const source = action.reason === "castle_rook_from_offset"
      ? `${locationLabel(action.source)} ${operation.active_route?.[1] || "offset"} corner`
      : locationLabel(action.source);
    els["transfer-action"].textContent = `${pieceDescription(action.piece)} · ${source} → ${locationLabel(action.destination)}`;
  } else if (action?.type === "rook_offset" || action?.type === "piece_offset") {
    const motion = action.direction === "OUT" ? "nudge toward" : "recenter from";
    els["transfer-action"].textContent = `${pieceDescription(action.piece)} · ${motion} ${action.corner} corner within ${action.square}`;
  } else if (action?.type === "manual_replacement") {
    els["transfer-action"].textContent = `Promotion at ${action.square}`;
  } else {
    els["transfer-action"].textContent = "Preparing the next transfer";
  }
  const route = Array.isArray(operation.active_route) ? operation.active_route : [];
  els["transfer-route"].hidden = route.length === 0;
  const command = action?.type === "rook_offset" || action?.type === "piece_offset" ? "OFFSET"
    : action?.reason === "castle_rook_from_offset" ? "CARRY_OFFSET" : "CARRY";
  els["transfer-route"].textContent = route.length ? `${command} arguments: ${route.join(" ")}` : "";
}

export function renderSetup() {
  if (!ui.config) return;
  const values = [
    ["Checkpoint", ui.config.state_path],
    ["Connection", ui.config.simulate ? "Simulation" : (ui.config.serial_port || "Physical port not set")],
    ["Stockfish", ui.config.stockfish_path],
    ["Geometry", ui.config.simulate ? ui.config.geometry_file : "Live Uno GEOM"],
    ["Serial log", ui.config.serial_log],
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

function parkingLocation(slot) {
  if (slot === 0) return "by a1 corner";
  if (slot === 8) return "by h1 corner";
  if (slot === 9) return "by a8 corner";
  if (slot === 17) return "by h8 corner";
  if (slot < 9) return `between ${FILES[slot - 1]}1 and ${FILES[slot]}1`;
  if (slot < 18) return `between ${FILES[slot - 10]}8 and ${FILES[slot - 9]}8`;
  if (slot < 25) return `between a${slot - 17} and a${slot - 16}`;
  return `between h${slot - 24} and h${slot - 23}`;
}

// Numbering follows the physical perimeter; a flipped view rotates each slot 180 degrees.
function parkingGridPosition(slot) {
  let column;
  let row;
  if (slot < 9) { column = slot + 1; row = 9; }
  else if (slot < 18) { column = slot - 8; row = 1; }
  else if (slot < 25) { column = 1; row = 26 - slot; }
  else { column = 9; row = 33 - slot; }
  return ui.whiteAtBottom ? [column, row] : [10 - column, 10 - row];
}

export function renderParking() {
  const slots = Array.isArray(ui.state.physical_parking) ? ui.state.physical_parking : [];
  const signature = JSON.stringify([slots, ui.whiteAtBottom]);
  if (signature === previousParkingSignature) return;
  previousParkingSignature = signature;
  const fragment = document.createDocumentFragment();
  const surface = document.createElement("div");
  surface.className = "parking-surface";
  surface.setAttribute("aria-hidden", "true");
  for (let row = 0; row < 8; row++) {
    for (let column = 0; column < 8; column++) {
      const square = document.createElement("span");
      square.className = `parking-mini-square ${(row + column) % 2 ? "dark" : "light"}`;
      surface.append(square);
    }
  }
  const surfaceLabel = document.createElement("span");
  surfaceLabel.className = "parking-surface-label";
  surfaceLabel.textContent = "PLAYING SURFACE";
  surface.append(surfaceLabel);
  fragment.append(surface);
  let occupied = 0;
  for (let slot = 0; slot < 32; slot++) {
    const symbol = slots[slot];
    const id = `P${String(slot).padStart(2, "0")}`;
    const description = `${id}, ${parkingLocation(slot)}: ${symbol ? pieceDescription(symbol) : "empty"}`;
    const [column, row] = parkingGridPosition(slot);
    const cell = document.createElement("div");
    cell.className = `parking-slot${symbol ? ` occupied ${symbol === symbol.toUpperCase() ? "white" : "black"}` : ""}`;
    cell.style.gridColumn = String(column);
    cell.style.gridRow = String(row);
    cell.setAttribute("role", "img");
    cell.setAttribute("aria-label", description);
    cell.title = description;
    if (symbol) {
      occupied += 1;
      cell.append(pieceIcon(symbol));
    }
    const number = document.createElement("small");
    number.textContent = id;
    cell.append(number);
    fragment.append(cell);
  }
  els.parking.replaceChildren(fragment);
  els.parking.setAttribute("aria-label", `Expected physical parking locations, ${ui.whiteAtBottom ? "White" : "Black"} side at bottom`);
  els["parking-orientation"].textContent = ui.whiteAtBottom
    ? "White side at bottom · a-file on the left"
    : "Black side at bottom · h-file on the left";
  els["parking-count"].textContent = `${occupied} / 32`;
}

function renderHistory() {
  const moves = Array.isArray(ui.state.moves) ? ui.state.moves : [];
  const san = Array.isArray(ui.state.moves_san) && ui.state.moves_san.length === moves.length ? ui.state.moves_san : moves;
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

// Inspection is required when the worker ended with an uncertain physical action.
export function renderRecovery() {
  const needed = needsReconciliation();
  els["recovery-panel"].hidden = !needed;
  if (!needed) return;
  const operation = activeOperation();
  const isReset = Boolean(ui.state.reset);
  const action = operation?.action || operation?.actions?.[operation?.next_action] || null;
  const signature = JSON.stringify([isReset, operation?.uci, operation?.next_action, operation?.phase, action]);
  if (signature !== previousRecoverySignature) {
    previousRecoverySignature = signature;
    els.inspected.checked = false;
  }
  const index = Number.isInteger(operation?.next_action) ? operation.next_action + 1 : "?";
  const total = operation?.total_actions || "?";
  const target = isReset ? "board reset" : operation?.uci ? `move ${operation.uci}` : "physical move";
  els["recovery-detail"].textContent = `The ${target} stopped during action ${index} of ${total}. The saved position cannot establish where the piece ended up.`;
  if (action?.type === "transfer") {
    const source = action.reason === "castle_rook_from_offset"
      ? `${locationLabel(action.source)} ${operation.active_route?.[1] || "offset"} corner`
      : locationLabel(action.source);
    els["recovery-action"].textContent = `${pieceDescription(action.piece)}: ${source} → ${locationLabel(action.destination)}`;
  } else if (action?.type === "rook_offset" || action?.type === "piece_offset") {
    const source = action.direction === "OUT" ? "center" : `${action.corner} corner`;
    const destination = action.direction === "OUT" ? `${action.corner} corner` : "center";
    els["recovery-action"].textContent = `${pieceDescription(action.piece)} in ${action.square}: ${source} → ${destination}`;
  } else if (action?.type === "manual_replacement") {
    els["recovery-action"].textContent = `Inspect promotion at ${action.square}`;
  } else {
    els["recovery-action"].textContent = "Inspect the last transfer and its parking slots.";
  }
  const isOffset = action?.type === "rook_offset" || action?.type === "piece_offset";
  const isOffsetCarry = action?.reason === "castle_rook_from_offset";
  const instruction = els["recovery-panel"].querySelector(".recovery-instruction");
  instruction.textContent = isOffset
    ? "Inspect the named piece within its square and check nearby pieces. Confirm whether it is centered or at the named corner before choosing the outcome."
    : isOffsetCarry
    ? "Inspect the rook at its offset corner, its destination, and the king's square before choosing the outcome."
    : "Check the piece, its source and destination, and all affected parking slots against the physical board before choosing the outcome.";
  const sourceButton = els["recovery-panel"].querySelector('[data-outcome="source"]');
  const destinationButton = els["recovery-panel"].querySelector('[data-outcome="destination"]');
  sourceButton.textContent = isOffset ? (action.direction === "OUT" ? "Piece at center" : "Piece at corner")
    : isOffsetCarry ? "Rook at corner" : "Piece at source";
  destinationButton.textContent = isOffset ? (action.direction === "OUT" ? "Piece at corner" : "Piece at center") : "Piece at destination";
  const disabled = !els.inspected.checked || ui.commandInFlight || !ui.online || ui.state.busy;
  for (const button of els["recovery-panel"].querySelectorAll("[data-outcome]")) button.disabled = disabled;
}

export function renderPromotionDialog() {
  if (!ui.promotionMoves.length) return;
  const fragment = document.createDocumentFragment();
  for (const role of ["q", "r", "b", "n"]) {
    const move = ui.promotionMoves.find(candidate => candidate.endsWith(role));
    if (!move) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "promotion-choice";
    button.dataset.move = move;
    const glyph = pieceToken(ui.state.turn === "black" ? role : role.toUpperCase(), "promotion-token");
    const label = document.createElement("small");
    label.textContent = PROMOTION_NAMES[role];
    button.append(glyph, label);
    fragment.append(button);
  }
  els["promotion-options"].replaceChildren(fragment);
  els["promotion-dialog"].hidden = false;
  els["promotion-options"].querySelector("button")?.focus();
}

export function closePromotionDialog() {
  els["promotion-dialog"].hidden = true;
  ui.promotionMoves = [];
  els.board.querySelector("button:not(:disabled)")?.focus();
}
