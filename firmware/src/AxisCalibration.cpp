#include "AxisCalibration.h"
#include "config.h"

bool AxisCalibration::isActive() const { return stage_ != Stage::Idle; }
float AxisCalibration::travelMm() const {
    return valid_ ? spanPulses_ / Config::PULSES_PER_MM : 0.0f;
}

bool AxisCalibration::start(float savedSpanMm) {
    if (isActive() || axis_.isRunning()) return false;
    valid_ = false;
    error_ = nullptr;
    referenceOnly_ = savedSpanMm > 0;
    if (referenceOnly_) {
        if (!isfinite(savedSpanMm) || savedSpanMm < Config::CALIBRATION_MIN_TRAVEL_MM ||
            savedSpanMm > Config::CALIBRATION_MAX_TRAVEL_MM) return false;
        spanPulses_ = lroundf(savedSpanMm * Config::PULSES_PER_MM);
    }
    if (!axis_.isReady() || !axis_.minLimitEnabled() || !axis_.maxLimitEnabled()) {
        error_ = F("Enable and verify both wired NO limit inputs first.");
        return false;
    }
    if (axis_.minLimitTriggered() && axis_.maxLimitTriggered()) {
        error_ = F("Both limits are triggered; check wiring.");
        return false;
    }
    savedSpeed_ = axis_.speedMmPerSecond();
    if (axis_.minLimitTriggered()) {
        move(Stage::BackoffMin, Config::CALIBRATION_BACKOFF_MM,
             Config::CALIBRATION_LATCH_MM_PER_SEC);
    } else if (axis_.maxLimitTriggered()) {
        move(Stage::ClearMax, -Config::CALIBRATION_BACKOFF_MM,
             Config::CALIBRATION_LATCH_MM_PER_SEC);
    } else {
        move(Stage::SeekMin, -Config::CALIBRATION_MAX_TRAVEL_MM,
             Config::CALIBRATION_SEEK_MM_PER_SEC);
    }
    return isActive();
}

void AxisCalibration::restoreSpeed() { axis_.setSpeedMmPerSecond(savedSpeed_); }

void AxisCalibration::fail(const __FlashStringHelper *message) {
    axis_.stop();
    restoreSpeed();
    stage_ = Stage::Idle;
    valid_ = false;
    error_ = message;
}

void AxisCalibration::cancel() {
    if (isActive()) fail(F("Canceled; measurement discarded."));
}

void AxisCalibration::move(Stage stage, float mm, float speed) {
    stage_ = stage;
    stageStartedMs_ = millis();
    if (!axis_.setSpeedMmPerSecond(speed) || !axis_.moveMm(mm)) {
        fail(F("Calibration move rejected."));
    }
}

void AxisCalibration::confirm(Stage stage) {
    // Stop on the FIRST triggered edge; debounce only while stationary.
    axis_.stop();
    stage_ = stage;
    stageStartedMs_ = millis();
    stableSinceMs_ = stageStartedMs_;
    stable_ = true;
}

void AxisCalibration::update() {
    if (!isActive()) return;
    const bool min = axis_.minLimitTriggered();
    const bool max = axis_.maxLimitTriggered();
    const uint32_t now = millis();
    if (min && max) { fail(F("Both limits triggered during calibration.")); return; }
    if (static_cast<uint32_t>(now - stageStartedMs_) >= Config::CALIBRATION_STAGE_TIMEOUT_MS) {
        fail(F("Calibration stage timed out.")); return;
    }

    // The opposite end may only be triggered in stages explicitly backing off it.
    const bool minSide = stage_ == Stage::SeekMin || stage_ == Stage::ConfirmMin ||
        stage_ == Stage::BackoffMin || stage_ == Stage::LatchMin ||
        stage_ == Stage::ConfirmMinLatch || stage_ == Stage::LeaveMin;
    if ((minSide && max) || (!minSide && min)) {
        fail(F("Unexpected opposite limit; check direction and wiring.")); return;
    }

    const bool confirming = stage_ == Stage::ConfirmMin || stage_ == Stage::ConfirmMinLatch ||
        stage_ == Stage::ConfirmMax || stage_ == Stage::ConfirmMaxLatch;
    if (confirming) {
        const bool triggered = minSide ? min : max;
        if (!triggered) stable_ = false;
        else if (!stable_) { stable_ = true; stableSinceMs_ = now; }
        if (static_cast<uint32_t>(now - stageStartedMs_) >= Config::SWITCH_CONFIRM_TIMEOUT_MS) {
            fail(F("Switch did not stay triggered after contact.")); return;
        }
        if (!stable_ || static_cast<uint32_t>(now - stableSinceMs_) < Config::SWITCH_SETTLE_MS)
            return;
        switch (stage_) {
            case Stage::ConfirmMin:
                move(Stage::BackoffMin, Config::CALIBRATION_BACKOFF_MM,
                     Config::CALIBRATION_LATCH_MM_PER_SEC); break;
            case Stage::ConfirmMinLatch:
                if (!axis_.setPositionPulses(0)) { fail(F("Could not zero MIN position.")); break; }
                move(Stage::LeaveMin, Config::CALIBRATION_BACKOFF_MM,
                     Config::CALIBRATION_LATCH_MM_PER_SEC); break;
            case Stage::ConfirmMax:
                move(Stage::BackoffMax, -Config::CALIBRATION_BACKOFF_MM,
                     Config::CALIBRATION_LATCH_MM_PER_SEC); break;
            case Stage::ConfirmMaxLatch: {
                spanPulses_ = axis_.positionPulses();
                const float span = spanPulses_ / Config::PULSES_PER_MM;
                if (span < Config::CALIBRATION_MIN_TRAVEL_MM ||
                    span > Config::CALIBRATION_MAX_TRAVEL_MM) {
                    fail(F("Measured span is outside configured bounds.")); break;
                }
                move(Stage::Park, Config::END_CLEARANCE_MM - span,
                     Config::CALIBRATION_SEEK_MM_PER_SEC);
                break;
            }
            default: break;
        }
        return;
    }

    if (stage_ == Stage::SeekMin || stage_ == Stage::LatchMin) {
        if (min) { confirm(stage_ == Stage::SeekMin ? Stage::ConfirmMin : Stage::ConfirmMinLatch); return; }
    } else if (stage_ == Stage::SeekMax || stage_ == Stage::LatchMax) {
        if (max) { confirm(stage_ == Stage::SeekMax ? Stage::ConfirmMax : Stage::ConfirmMaxLatch); return; }
    } else if (stage_ == Stage::Park && max &&
               spanPulses_ - axis_.positionPulses() >
                   Config::CALIBRATION_BACKOFF_MM * Config::PULSES_PER_MM) {
        fail(F("MAX did not release while parking.")); return;
    }
    if (axis_.isRunning()) return;
    switch (stage_) {
        case Stage::ClearMax:
            if (max) { fail(F("MAX did not release; check switch/backoff.")); break; }
            move(Stage::SeekMin, -Config::CALIBRATION_MAX_TRAVEL_MM,
                 Config::CALIBRATION_SEEK_MM_PER_SEC); break;
        case Stage::BackoffMin:
            if (min) { fail(F("MIN did not release; check switch/backoff.")); break; }
            move(Stage::LatchMin, -2 * Config::CALIBRATION_BACKOFF_MM,
                 Config::CALIBRATION_LATCH_MM_PER_SEC); break;
        case Stage::LeaveMin:
            if (min) { fail(F("MIN did not release after latch.")); break; }
            if (referenceOnly_) {
                move(Stage::Park, Config::END_CLEARANCE_MM -
                     axis_.positionPulses() / Config::PULSES_PER_MM,
                     Config::CALIBRATION_LATCH_MM_PER_SEC);
            } else move(Stage::SeekMax, Config::CALIBRATION_MAX_TRAVEL_MM,
                        Config::CALIBRATION_SEEK_MM_PER_SEC);
            break;
        case Stage::BackoffMax:
            if (max) { fail(F("MAX did not release; check switch/backoff.")); break; }
            move(Stage::LatchMax, 2 * Config::CALIBRATION_BACKOFF_MM,
                 Config::CALIBRATION_LATCH_MM_PER_SEC); break;
        case Stage::Park:
            if (max || min) { fail(F("End clearance did not release switches.")); break; }
            restoreSpeed();
            stage_ = Stage::Idle;
            valid_ = true;
            break;
        default: fail(F("Limit not reached within search distance.")); break;
    }
}
