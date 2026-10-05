#include <Arduino.h>
#include <string.h>
#include "BoardController.h"
#include "config.h"

namespace {
FastAccelStepperEngine engine;
LinearAxis xAxis(Config::X_STEP_PIN, Config::X_DIR_PIN,
                 Config::X_MIN_LIMIT_PIN, Config::X_MAX_LIMIT_PIN,
                 Config::X_MIN_LIMIT_ENABLED, Config::X_MAX_LIMIT_ENABLED,
                 Config::X_OUTWARD_DIR_LEVEL);
LinearAxis yAxis(Config::Y_STEP_PIN, Config::Y_DIR_PIN,
                 Config::Y_MIN_LIMIT_PIN, Config::Y_MAX_LIMIT_PIN,
                 Config::Y_MIN_LIMIT_ENABLED, Config::Y_MAX_LIMIT_ENABLED,
                 Config::Y_OUTWARD_DIR_LEVEL);
LinearAxis *const axes[] = {&xAxis, &yAxis};
AxisCalibration xCalibration(xAxis), yCalibration(yAxis);
AxisCalibration *const calibrations[] = {&xCalibration, &yCalibration};
Electromagnet electromagnet(Config::ELECTROMAGNET_RELAY_PIN);
BoardController board(xAxis, yAxis, xCalibration, yCalibration, electromagnet);

enum class Pending : uint8_t { None, Home, Ref, Carry };
Pending pending = Pending::None;
uint16_t pendingId = 0, lastRequestId = 0;
bool hasRequestId = false;
int8_t homingAxis = -1;
char frame[96];
uint8_t frameLength = 0;
bool framedInput = false, frameOverflow = false;

void prefix(uint16_t id) { Serial.print('@'); Serial.print(id); Serial.print(' '); }
void ack(uint16_t id) { prefix(id); Serial.println(F("ACK")); }
void error(uint16_t id, const __FlashStringHelper *code) {
    prefix(id); Serial.print(F("ERR ")); Serial.println(code);
}
void done(uint16_t id, const __FlashStringHelper *payload) {
    prefix(id); Serial.print(F("DONE ")); Serial.println(payload);
}
bool busy() {
    return homingAxis >= 0 || board.isActive() || xAxis.isRunning() || yAxis.isRunning();
}

bool startHoming(bool saved) {
    if (saved && !board.hasSavedGeometry()) return false;
    board.invalidate();
    electromagnet.off();
    for (uint8_t i = 0; i < 2; ++i) {
        if (!axes[i]->isReady() || !axes[i]->minLimitEnabled() || !axes[i]->maxLimitEnabled() ||
            (axes[i]->minLimitTriggered() && axes[i]->maxLimitTriggered())) return false;
    }
    if (!xCalibration.start(saved ? board.savedSpan(0) : 0)) return false;
    homingAxis = 0;
    return true;
}

void updateHoming() {
    if (homingAxis < 0) return;
    const uint8_t i = homingAxis;
    calibrations[i]->update();
    if (calibrations[i]->isActive()) return;
    if (!calibrations[i]->isCalibrated()) {
        board.invalidate(); homingAxis = -1;
        error(pendingId, i == 0 ? F("CAL_X") : F("CAL_Y"));
        pending = Pending::None; pendingId = 0;
        return;
    }
    if (i == 0) {
        if (yCalibration.start(pending == Pending::Ref ? board.savedSpan(1) : 0)) homingAxis = 1;
        else {
            board.invalidate(); homingAxis = -1;
            error(pendingId, F("CAL_Y")); pending = Pending::None; pendingId = 0;
        }
        return;
    }
    homingAxis = -1;
    if (board.generate(pending == Pending::Ref))
        done(pendingId, pending == Pending::Ref ? F("REF") : F("HOME"));
    else error(pendingId, F("GEOMETRY"));
    pending = Pending::None; pendingId = 0;
}

int8_t hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
int8_t halfCoordinate(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'G') return c - 'A' + 10;
    return -1;
}
bool locationArg(char *&cursor, uint8_t &location) {
    if (*cursor < '0' || *cursor > '9') return false;
    uint16_t value = 0;
    do {
        value = value * 10 + *cursor++ - '0';
        if (value > 95) return false;
    } while (*cursor >= '0' && *cursor <= '9');
    location = value;
    return true;
}

void processFrame() {
    frame[frameLength] = '\0';
    char *p = frame;
    if (*p < '0' || *p > '9') { Serial.println(F("!ERR BAD_ID")); return; }
    uint32_t value = 0;
    do {
        value = value * 10 + *p++ - '0';
        if (value > 65535) { Serial.println(F("!ERR BAD_ID")); return; }
    } while (*p >= '0' && *p <= '9');
    if (!value) { Serial.println(F("!ERR BAD_ID")); return; }
    const uint16_t id = static_cast<uint16_t>(value);
    if (pendingId == id) { ack(id); return; }
    ack(id);
    // Host IDs increase without wrapping during one boot. STOP may use 65535.
    if (hasRequestId && id <= lastRequestId) { error(id, F("DUPLICATE")); return; }
    lastRequestId = id; hasRequestId = true;
    if (frameOverflow) { error(id, F("LENGTH")); return; }
    if (*p++ != ' ' || *p == '\0') { error(id, F("SYNTAX")); return; }
    char *verb = p;
    while (*p && *p != ' ') {
        if (*p < 'A' || *p > 'Z') { error(id, F("SYNTAX")); return; }
        ++p;
    }
    char *args = p;
    if (*p) { *p++ = '\0'; args = p; }

    if (strcmp(verb, "MAP") == 0) {
        if (busy()) { error(id, F("BUSY")); return; }
        if (strlen(args) != 24) { error(id, F("SYNTAX")); return; }
        uint8_t bytes[12];
        for (uint8_t i = 0; i < 12; ++i) {
            const int8_t high = hexNibble(args[2 * i]), low = hexNibble(args[2 * i + 1]);
            if (high < 0 || low < 0) { error(id, F("SYNTAX")); return; }
            bytes[i] = (high << 4) | low;
        }
        if (!board.loadMap(bytes)) error(id, F("BUSY"));
        else done(id, F("MAP"));
        return;
    }
    if (strcmp(verb, "CARRY") == 0) {
        uint8_t source, destination;
        if (!locationArg(args, source) || *args++ != ' ' ||
            !locationArg(args, destination) || *args++ != ' ') {
            error(id, F("SYNTAX")); return;
        }
        const uint8_t length = strlen(args);
        if (length < 4 || length > 2 * BoardController::MAX_WAYPOINTS || (length & 1)) {
            error(id, F("SYNTAX")); return;
        }
        HalfSquarePoint points[BoardController::MAX_WAYPOINTS];
        for (uint8_t i = 0; i < length / 2; ++i) {
            const int8_t x = halfCoordinate(args[2 * i]), y = halfCoordinate(args[2 * i + 1]);
            if (x < 0 || y < 0) { error(id, F("SYNTAX")); return; }
            points[i] = {x, y};
        }
        const BoardController::StartError result =
            board.startCarry(source, destination, points, length / 2);
        switch (result) {
            case BoardController::StartError::None:
                pending = Pending::Carry; pendingId = id; return;
            case BoardController::StartError::Busy: error(id, F("BUSY")); return;
            case BoardController::StartError::Reference: error(id, F("REFERENCE")); return;
            case BoardController::StartError::Map: error(id, F("MAP")); return;
            case BoardController::StartError::Route: error(id, F("ROUTE")); return;
            case BoardController::StartError::Geometry: error(id, F("GEOMETRY")); return;
        }
    }
    if (*args) { error(id, F("SYNTAX")); return; }
    if (strcmp(verb, "HELLO") == 0) {
        if (busy()) { error(id, F("BUSY")); return; }
        done(id, F("HELLO V=1 CAPS=STATUS,HOME,REF,STOP,MAP,CARRY,SAVE,GEOM"));
    } else if (strcmp(verb, "STATUS") == 0) {
        prefix(id); Serial.print(F("DONE STATUS REF=")); Serial.print(board.referenced() ? '1' : '0');
        Serial.print(F(" BUSY=")); Serial.print(busy() ? '1' : '0');
        Serial.print(F(" MAP=")); Serial.print(board.mapKnown() ? '1' : '0');
        Serial.print(F(" SAVED=")); Serial.println(board.hasSavedGeometry() ? '1' : '0');
    } else if (strcmp(verb, "GEOM") == 0) {
        if (busy()) { error(id, F("BUSY")); return; }
        if (!board.referenced()) { error(id, F("REFERENCE")); return; }
        prefix(id); Serial.print(F("DONE GEOM X=")); Serial.print(xCalibration.travelMm(), 4);
        Serial.print(F(" Y=")); Serial.print(yCalibration.travelMm(), 4);
        Serial.print(F(" OX=")); Serial.print(board.boardOriginXmm(), 4);
        Serial.print(F(" OY=")); Serial.print(board.boardOriginYmm(), 4);
        Serial.print(F(" P=")); Serial.print(Config::BOARD_SQUARE_MM, 4);
        Serial.print(F(" I=")); Serial.print(Config::END_CLEARANCE_MM, 4);
        Serial.print(F(" PPM=")); Serial.println(Config::PULSES_PER_MM, 4);
    } else if (strcmp(verb, "STOP") == 0) {
        board.cancel(); xCalibration.cancel(); yCalibration.cancel(); homingAxis = -1;
        if (pendingId) error(pendingId, F("CANCELED"));
        pending = Pending::None; pendingId = 0;
        done(id, F("STOP"));
    } else if (strcmp(verb, "SAVE") == 0) {
        if (busy()) error(id, F("BUSY"));
        else if (!board.referenced()) error(id, F("REFERENCE"));
        else { board.save(); done(id, F("SAVE")); }
    } else if (strcmp(verb, "HOME") == 0 || strcmp(verb, "REF") == 0) {
        if (busy()) { error(id, F("BUSY")); return; }
        const bool saved = verb[0] == 'R';
        if (saved && !board.hasSavedGeometry()) { error(id, F("NO_GEOMETRY")); return; }
        if (!startHoming(saved)) { error(id, F("REJECTED")); return; }
        pending = saved ? Pending::Ref : Pending::Home; pendingId = id;
    } else error(id, F("UNKNOWN"));
}

void readCommand() {
    if (Serial.available() == 0) return;
    const char c = Serial.read();
    if (!framedInput) {
        if (c == '@') { framedInput = true; frameLength = 0; frameOverflow = false; }
        // One-byte bench stop remains available outside frames.
        else if (c == 'x') {
            board.cancel(); xCalibration.cancel(); yCalibration.cancel(); homingAxis = -1;
            if (pendingId) error(pendingId, F("CANCELED"));
            pending = Pending::None; pendingId = 0;
        }
        return;
    }
    if (c == '\r' || c == '\n') {
        processFrame(); framedInput = false; frameLength = 0; frameOverflow = false;
    } else if (frameLength < sizeof(frame) - 1) frame[frameLength++] = c;
    else frameOverflow = true;
}
} // namespace

void setup() {
    electromagnet.begin();
    xAxis.initializePins(); yAxis.initializePins();
    Serial.begin(Config::SERIAL_BAUD);
    engine.init();
    FastAccelStepper *xMotor = engine.stepperConnectToPin(xAxis.stepPin());
    FastAccelStepper *yMotor = engine.stepperConnectToPin(yAxis.stepPin());
    xAxis.begin(xMotor); yAxis.begin(yMotor);
    Serial.println(F("!BOOT V=1"));
    board.loadSaved();
}

void loop() {
    updateHoming();
    board.update();
    if (pending == Pending::Carry) {
        const BoardController::Result result = board.takeResult();
        if (result != BoardController::Result::None) {
            if (result == BoardController::Result::Complete) done(pendingId, F("CARRY"));
            else error(pendingId, F("MOTION"));
            pending = Pending::None; pendingId = 0;
        }
    }
    readCommand();
}
