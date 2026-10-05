#include "BoardController.h"

// Geometry, saved calibration, and the host-declared occupancy map.

BoardController::BoardController(LinearAxis &x, LinearAxis &y, AxisCalibration &xc,
                                 AxisCalibration &yc, Electromagnet &magnet)
    : axes_{&x, &y}, calibrations_{&xc, &yc}, magnet_(magnet) {}

void BoardController::invalidate() {
    calibrations_[0]->invalidate();
    calibrations_[1]->invalidate();
}

bool BoardController::referenced() const {
    return generated_ && calibrations_[0]->isCalibrated() && calibrations_[1]->isCalibrated();
}

bool BoardController::loadMap(const uint8_t bytes[12]) {
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) return false;
    pieces_.load(bytes);
    return true;
}

void BoardController::loadSaved() {
    hasSaved_ = loadBoardGeometry(saved_);
    if (hasSaved_) {
        size_ = calculateUsableTableSize(saved_.span[0], saved_.span[1]);
        size_.boardOriginXmm = saved_.origin[0];
        size_.boardOriginYmm = saved_.origin[1];
        generated_ = true;
    }
    Serial.println(hasSaved_ ? F("EEPROM geometry loaded; use REF or HOME.") :
                              F("No saved geometry; use HOME then SAVE."));
}

void BoardController::save() {
    if (!referenced()) { Serial.println(F("Use HOME or REF before SAVE.")); return; }
    for (uint8_t i = 0; i < 2; ++i) saved_.span[i] = calibrations_[i]->travelMm();
    saved_.origin[0] = size_.boardOriginXmm; saved_.origin[1] = size_.boardOriginYmm;
    saveBoardGeometry(saved_);
    hasSaved_ = true;
    Serial.println(F("Geometry saved to EEPROM."));
}

bool BoardController::generate(bool useSaved) {
    generated_ = false;
    if (!calibrations_[0]->isCalibrated() || !calibrations_[1]->isCalibrated()) return false;
    size_ = calculateUsableTableSize(calibrations_[0]->travelMm(), calibrations_[1]->travelMm());
    if (useSaved && hasSaved_) {
        size_.boardOriginXmm = saved_.origin[0];
        size_.boardOriginYmm = saved_.origin[1];
    }
    if (!size_.valid || !size_.squareCentersFit) {
        Serial.println(F("Board centers do not fit; pitch unchanged. Board moves disabled."));
        return false;
    }
    // Also account for pulse rounding at the four extreme center coordinates.
    for (uint8_t corner = 0; corner < 2; ++corner) {
        const BoardPoint p = squareCenter(size_, corner * 7, corner * 7);
        if (!inside(0, lroundf(p.x * Config::PULSES_PER_MM)) ||
            !inside(1, lroundf(p.y * Config::PULSES_PER_MM))) return false;
    }
    generated_ = true;
    Serial.println(F("Board ready: a1 nearest MIN/MIN; files +X, ranks +Y."));
    return true;
}

bool BoardController::inside(uint8_t axis, int32_t pulses) const {
    const int32_t low = static_cast<int32_t>(ceilf(Config::END_CLEARANCE_MM * Config::PULSES_PER_MM));
    const int32_t high = static_cast<int32_t>(floorf(
        (calibrations_[axis]->travelMm() - Config::END_CLEARANCE_MM) * Config::PULSES_PER_MM));
    return pulses >= low && pulses <= high;
}

bool BoardController::preflight() {
    // A route must begin at a known motor coordinate inside the measured inset.
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) {
        Serial.println(F("Board busy; wait or STOP.")); return false;
    }
    if (!generated_ || !calibrations_[0]->isCalibrated() || !calibrations_[1]->isCalibrated()) {
        Serial.println(F("Board unavailable; use HOME or REF.")); return false;
    }
    for (uint8_t i = 0; i < 2; ++i) {
        if (!axes_[i]->isReady() || !inside(i, axes_[i]->positionPulses())) {
            fail(F("Board position/limit invalid; use HOME.")); return false;
        }
    }
    for (uint8_t i = 0; i < 2; ++i) savedSpeed_[i] = axes_[i]->speedMmPerSecond();
    magnet_.off();
    return true;
}
