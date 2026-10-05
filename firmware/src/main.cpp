#include <Arduino.h>
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
int8_t homingAxis = -1;
bool referenceSaved = false;
char lineCommand = 0, arguments[4];
uint8_t argumentCount = 0;
bool overflow = false, discardLine = false;

bool busy() {
    return homingAxis >= 0 || board.isActive() || xAxis.isRunning() || yAxis.isRunning();
}

void printStatus() {
    board.printState();
    for (uint8_t i = 0; i < 2; ++i) {
        Serial.print(i == 0 ? F("X: ") : F("Y: "));
        Serial.print(axes[i]->isReady() ? F("ready; MIN=") : F("FAULT; MIN="));
        Serial.print(!axes[i]->minLimitEnabled() ? F("disabled") :
            axes[i]->minLimitTriggered() ? F("TRIGGERED") : F("released"));
        Serial.print(F("; MAX="));
        Serial.println(!axes[i]->maxLimitEnabled() ? F("disabled") :
            axes[i]->maxLimitTriggered() ? F("TRIGGERED") : F("released"));
    }
}

void printHelp() {
    Serial.println(F("\nXY chess: board/knight/parking tests"));
    Serial.println(F("X PUL=D9 DIR=D3 MIN=D4 MAX=D7; Y PUL=D10 DIR=D5 MIN=D6 MAX=D8"));
    Serial.println(F("h=home both; t=dimensions; q=coordinates; n=next; w=64-square walk"));
    Serial.println(F("ga1<Enter>=travel; db1c3<Enter>=dry knight; kb1c3<Enter>=carry knight"));
    Serial.println(F("s=save geometry; r=reference saved MIN; pa218/ua218=park/retrieve + Enter"));
    Serial.println(F("f=map starting position; e=map empty; v=map; ia11/i001=edit + Enter"));
    Serial.println(F("j=next full-board test leg; x=stop/unreference; o=OFF (idle keeps home); ?=status"));
    Serial.print(F("Travel/piece mm/s: ")); Serial.print(Config::TRAVEL_SPEED_MM_PER_SEC, 1);
    Serial.print('/'); Serial.println(Config::PIECE_SPEED_MM_PER_SEC, 1);
    printStatus();
}

void printTableSize() {
    if (!xCalibration.isCalibrated() || !yCalibration.isCalibrated()) {
        Serial.println(F("Run h first.")); return;
    }
    const TableSize size = calculateUsableTableSize(xCalibration.travelMm(), yCalibration.travelMm());
    Serial.print(F("Usable X,Y (mm): ")); Serial.print(size.usableXmm, 3);
    Serial.print(','); Serial.println(size.usableYmm, 3);
    Serial.print(F("Board origin X,Y from MIN (mm): ")); Serial.print(size.boardOriginXmm, 3);
    Serial.print(','); Serial.println(size.boardOriginYmm, 3);
    Serial.print(F("Pitch (mm): ")); Serial.println(Config::BOARD_SQUARE_MM, 3);
    Serial.println(size.squareCentersFit ? F("64 centers fit; verify dimensions with a ruler.") :
                                          F("Centers do not fit; pitch unchanged."));
}

void stopAll() {
    // Drop magnet and flush both queues before any serial output can block.
    board.cancel();
    xCalibration.cancel(); yCalibration.cancel();
    homingAxis = -1;
    Serial.println(F("Stopped; magnet OFF. Run h before board moves."));
}

void reportCalibration(uint8_t i) {
    Serial.print(i == 0 ? F("X: ") : F("Y: "));
    if (calibrations[i]->isCalibrated()) {
        Serial.print(F("Calibration complete. Travel (mm): "));
        Serial.println(calibrations[i]->travelMm(), 3);
    } else {
        Serial.print(F("Calibration failed: "));
        Serial.println(calibrations[i]->errorMessage());
    }
}

void startHoming(bool saved) {
    if (saved && !board.hasSavedGeometry()) { Serial.println(F("No saved geometry; h then s.")); return; }
    referenceSaved = saved;
    // Check both motors/switch configurations before either axis moves.
    board.invalidate();
    electromagnet.off();
    for (uint8_t i = 0; i < 2; ++i) {
        if (!axes[i]->isReady() || !axes[i]->minLimitEnabled() || !axes[i]->maxLimitEnabled() ||
            (axes[i]->minLimitTriggered() && axes[i]->maxLimitTriggered())) {
            Serial.println(F("Homing rejected: verify motors and all four limit inputs.")); return;
        }
    }
    if (xCalibration.start(saved ? board.savedSpan(0) : 0)) {
        homingAxis = 0;
        Serial.println(saved ? F("Referencing saved X/Y MIN; magnet OFF.") : F("Homing X then Y; magnet OFF."));
    } else reportCalibration(0);
}

void updateHoming() {
    if (homingAxis < 0) return;
    const uint8_t i = homingAxis;
    calibrations[i]->update();
    if (calibrations[i]->isActive()) return;
    reportCalibration(i);
    if (!calibrations[i]->isCalibrated()) { board.invalidate(); homingAxis = -1; return; }
    if (i == 0) {
        if (yCalibration.start(referenceSaved ? board.savedSpan(1) : 0)) homingAxis = 1;
        else { reportCalibration(1); board.invalidate(); homingAxis = -1; }
    } else {
        homingAxis = -1;
        printTableSize();
        board.generate(referenceSaved);
    }
}

bool commandReady() {
    if (!busy()) return true;
    Serial.println(F("Busy; wait or x.")); return false;
}

void finishLine() {
    const char command = lineCommand;
    lineCommand = 0;
    uint8_t fromFile = 0, fromRank = 0, toFile = 0, toRank = 0;
    const uint8_t required = command == 'g' ? 2 : command == 'i' ? 3 : 4;
    bool valid = !overflow && argumentCount == required;
    uint8_t slot = 255;
    if (command == 'p' || command == 'u') {
        if (arguments[2] >= '0' && arguments[2] <= '3' && arguments[3] >= '0' && arguments[3] <= '9')
            slot = (arguments[2] - '0') * 10 + arguments[3] - '0';
        valid &= slot < 32 && parseSquare(arguments, fromFile, fromRank);
    } else if (command == 'i') {
        if (parseSquare(arguments, fromFile, fromRank)) slot = fromRank * 8 + fromFile;
        else if (arguments[0] >= '0' && arguments[0] <= '3' && arguments[1] >= '0' && arguments[1] <= '9') {
            slot = (arguments[0] - '0') * 10 + arguments[1] - '0';
            slot = slot < 32 ? slot + 64 : 255;
        }
        valid &= slot < 96 && (arguments[2] == '0' || arguments[2] == '1');
    } else {
        valid &= parseSquare(arguments, fromFile, fromRank);
        if (command != 'g') valid &= parseSquare(arguments + 2, toFile, toRank);
    }
    if (!valid) { Serial.println(F("Invalid input; ? for syntax.")); return; }
    if (!commandReady()) return;
    if (command == 'g') board.startSquare(fromFile, fromRank);
    else if (command == 'i') board.editPiece(slot, arguments[2] == '1');
    else if (command == 'p' || command == 'u') board.startParking(fromFile, fromRank, slot, command == 'u');
    else board.startKnight(fromFile, fromRank, toFile, toRank, command == 'k');
}

void readCommand() {
    // One byte per loop; no String, blocking parser, or queued movement commands.
    if (Serial.available() == 0) return;
    char c = Serial.read();
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c == 'x' || c == 'o') {
        lineCommand = 0; discardLine = false;
        if (c == 'o' && !busy()) {
            electromagnet.off(); Serial.println(F("Magnet OFF; idle reference retained.")); return;
        }
        stopAll(); return;
    }
    if (discardLine) { if (c == '\r' || c == '\n') discardLine = false; return; }
    if (lineCommand) {
        if (c == '\r' || c == '\n') finishLine();
        else if (c != ' ' && c != '\t') {
            if (argumentCount < sizeof(arguments)) arguments[argumentCount++] = c;
            else overflow = true;
        }
        return;
    }
    if (c == '\r' || c == '\n' || c == ' ' || c == '\t') return;
    if (c == '?') { if (busy()) printStatus(); else printHelp(); return; }
    if (c == 'g' || c == 'd' || c == 'k' || c == 'p' || c == 'u' || c == 'i') {
        lineCommand = c; argumentCount = 0; overflow = false; return;
    }
    if (c != 'h' && c != 't' && c != 'q' && c != 'n' && c != 'w' &&
        c != 's' && c != 'r' && c != 'f' && c != 'e' && c != 'v' && c != 'j') {
        Serial.println(F("Unknown command; ? for help.")); discardLine = true; return;
    }
    if (!commandReady()) return;
    switch (c) {
        case 'h': startHoming(false); break;
        case 'r': startHoming(true); break;
        case 's': board.save(); break;
        case 'f': board.resetPieces(true); break;
        case 'e': board.resetPieces(false); break;
        case 'v': board.printPieces(); break;
        case 'j': board.nextFixture(); break;
        case 't': printTableSize(); break;
        case 'q': board.printGrid(); break;
        case 'n': board.startWalk(false); break;
        case 'w': board.startWalk(true); break;
    }
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
    Serial.println(F("BOOT: carriage unreferenced; piece map unknown."));
    board.loadSaved();
    printHelp();
}

void loop() {
    updateHoming();
    board.update(); // Owns both axes and all four limits, including dwell stages.
    readCommand();
}
