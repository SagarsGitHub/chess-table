"use strict";

// Browser state and chess-display helpers shared by input handling and rendering.
export const PIECE_NAMES = { k: "king", q: "queen", r: "rook", b: "bishop", n: "knight", p: "pawn" };
export const PROMOTION_NAMES = { q: "Queen", r: "Rook", b: "Bishop", n: "Knight" };
export const FILES = "abcdefgh";
export const POLL_INTERVAL_MS = 1200;

export const els = Object.fromEntries([
  "board", "board-help", "board-view-description", "view-physical", "view-legal",
  "turn-indicator", "headline-state", "notice", "connection",
  "hardware-mode", "stop-button", "flip-button", "phase-tag", "session-status",
  "session-start", "session-live", "mode-select", "start-button", "active-mode",
  "move-count", "reset-button", "connection-detail", "parking", "parking-count",
  "parking-orientation",
  "transfer-status", "transfer-progress", "transfer-action", "transfer-route",
  "history", "history-count", "recovery-panel", "recovery-detail", "recovery-action",
  "inspected", "promotion-dialog", "promotion-options", "promotion-cancel", "setup-summary",
  "chesscom-account-form", "chesscom-username", "chesscom-load", "chesscom-search-form",
  "chesscom-month-field", "chesscom-month", "chesscom-query", "chesscom-query-label",
  "chesscom-search", "chesscom-status", "chesscom-error", "chesscom-games",
  "chesscom-empty-preview", "chesscom-preview", "chesscom-board", "chesscom-prev",
  "chesscom-forward", "chesscom-position", "chesscom-title", "chesscom-players",
  "chesscom-meta", "chesscom-link", "chesscom-reason", "chesscom-moves",
  "chesscom-replay-controls", "chesscom-confirm", "chesscom-replay-start",
  "chesscom-replay-next", "chesscom-replay-status",
  "chesscom-daily-controls", "chesscom-daily-confirm", "chesscom-daily-switch",
  "chesscom-follow-status", "chesscom-follow-title", "chesscom-daily-status",
  "chesscom-daily-times", "chesscom-daily-check", "chesscom-autoplay-controls",
  "chesscom-wait", "chesscom-replay-autoplay", "chesscom-automation-status",
  "chesscom-autoplay-status", "chesscom-replay-pause",
].map(id => [id, document.getElementById(id)]));

export const ui = {
  state: null,
  config: null,
  selected: null,
  dragOrigin: null,
  whiteAtBottom: true,
  viewOverride: null,
  commandInFlight: false,
  stopInFlight: false,
  online: false,
  localError: "",
  promotionMoves: [],
  chesscom: {
    kind: "historical",
    username: "ofroni",
    games: { daily: [], finished: [], historical: [] },
    loaded: { daily: false, finished: false, historical: false },
    archives: [],
    archiveUsername: null,
    contexts: {},
    selection: null,
    selectionKind: null,
    selectionToken: null,
    replay: null,
    daily: null,
    previewPly: null,
    loading: false,
    error: "",
    status: "Loading the historic game collection…",
  },
};

export function chesscomPositions(game = ui.chesscom.selection) {
  return Array.isArray(game?.positions) ? game.positions.filter(fen => typeof fen === "string") : [];
}

export function chesscomPlayer(player) {
  if (typeof player === "string") return player;
  return String(player?.username || player?.name || "Unknown player");
}

export function prettyError(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    return String(value.message || value.error || value.detail || JSON.stringify(value));
  }
  return "Request failed";
}

// Convert the logical FEN into square-to-piece entries for board rendering.
export function parseFen(fen) {
  const map = {};
  const placement = String(fen || "").split(" ")[0];
  const ranks = placement.split("/");
  if (ranks.length !== 8) return map;
  for (let row = 0; row < 8; row++) {
    let file = 0;
    for (const char of ranks[row]) {
      if (/[1-8]/.test(char)) file += Number(char);
      else if (PIECE_NAMES[char.toLowerCase()] && file < 8) {
        map[`${FILES[file]}${8 - row}`] = char;
        file += 1;
      }
    }
  }
  return map;
}

export function legalMoves() {
  return Array.isArray(ui.state?.legal_moves) ? ui.state.legal_moves.filter(move => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)) : [];
}

export function canMove() {
  if (!ui.state || !ui.online || ui.commandInFlight || ui.stopInFlight || ui.chesscom.replay?.active || ui.chesscom.daily?.active || needsReconciliation()) return false;
  if (typeof ui.state.can_move === "boolean") return ui.state.can_move;
  return ui.state.phase === "human_turn" || ui.state.phase === "manual_turn";
}

export function activeOperation() { return ui.state?.reset || ui.state?.pending || null; }
// An unchosen view follows the physical checkpoint while a transfer is active.
export function physicalView() { return ui.viewOverride === null ? Boolean(activeOperation()) : ui.viewOverride; }

// A saved in-flight action only needs inspection after its worker stops.
export function needsReconciliation() {
  if (!ui.state) return false;
  if (ui.state.busy) return false;
  const operation = activeOperation();
  if (operation && ["in_flight", "awaiting_confirmation"].includes(operation.phase)) return true;
  return /reconcil|uncertain/i.test(String(ui.state.phase || ""));
}
