#include "BoardController.h"
#include <util/atomic.h>

// Nonblocking movement, magnet timing, and fault teardown.

bool BoardController::moveTo(BoardPoint p, bool pieceProfile, bool tightHorizontal) {
    // Scale each axis's speed and acceleration by its share of the vector so
    // independent ramps finish together on orthogonal and diagonal segments.
    targets_[0] = lroundf(p.x * Config::PULSES_PER_MM);
    targets_[1] = lroundf(p.y * Config::PULSES_PER_MM);
    int32_t delta[2];
    for (uint8_t i = 0; i < 2; ++i) {
        if (!inside(i, targets_[i])) {
            fail(F("Board target outside measured bounds / limit active.")); return false;
        }
        delta[i] = targets_[i] - axes_[i]->positionPulses();
    }
    const float dx = static_cast<float>(delta[0]), dy = static_cast<float>(delta[1]);
    const float length = sqrtf(dx * dx + dy * dy);
    const float speed = tightHorizontal ? Config::TIGHT_HORIZONTAL_PIECE_SPEED_MM_PER_SEC :
        pieceProfile ? Config::PIECE_SPEED_MM_PER_SEC : Config::TRAVEL_SPEED_MM_PER_SEC;
    const float acceleration = tightHorizontal ? Config::TIGHT_HORIZONTAL_PIECE_ACCELERATION_MM_PER_SEC2 :
        pieceProfile ? Config::PIECE_ACCELERATION_MM_PER_SEC2 : Config::ACCELERATION_MM_PER_SEC2;
    for (uint8_t i = 0; i < 2; ++i) {
        if (delta[i] == 0) continue;
        const float fraction = fabsf(static_cast<float>(delta[i])) / length;
        if (!axes_[i]->setSpeedMmPerSecond(speed * fraction) ||
            !axes_[i]->setAccelerationMmPerSecond2(acceleration * fraction)) {
            fail(F("Board motion profile rejected.")); return false;
        }
    }
    Serial.print(F("Move X,Y (mm): ")); Serial.print(p.x, 3); Serial.print(','); Serial.print(p.y, 3);
    Serial.print(tightHorizontal ? F("; tight piece mm/s=") :
        pieceProfile ? F("; piece mm/s=") : F("; travel mm/s="));
    Serial.println(speed, 1);
    axes_[0]->resetBoardLimitFilter(); axes_[1]->resetBoardLimitFilter();
    bool accepted = true;
    // Submit both ramps before the AVR ramp-management interrupt can run.
    // These are proportional independent ramps, not a general CNC interpolator.
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        for (uint8_t i = 0; i < 2; ++i) {
            if (delta[i] != 0 && !axes_[i]->movePulses(delta[i], true)) { accepted = false; break; }
        }
        if (!accepted) { axes_[0]->stop(); axes_[1]->stop(); }
    }
    if (!accepted) { fail(F("Board move rejected; both axes stopped.")); return false; }
    stage_ = Stage::Moving;
    stageStartedMs_ = millis();
    return true;
}

void BoardController::waitForRelease() {
    // Allow a previously energized relay/magnet to release before fast travel.
    stage_ = Stage::TravelRelease;
    stageStartedMs_ = millis();
}

void BoardController::moveRoutePoint() {
    moveTo(routePoint(route_[routeIndex_]), routeIndex_ != 0, crowdedHorizontalSegment());
}

void BoardController::restoreProfiles() {
    for (uint8_t i = 0; i < 2; ++i) {
        axes_[i]->setAccelerationMmPerSecond2(Config::ACCELERATION_MM_PER_SEC2);
        axes_[i]->setSpeedMmPerSecond(savedSpeed_[i]);
    }
}

void BoardController::cancel() {
    axes_[0]->stop(); axes_[1]->stop(); magnet_.off();
    if (isActive()) restoreProfiles();
    // A canceled carry may have moved the piece partway; the host must MAP
    // again after reconciling the physical board.
    if (operation_ == Operation::Carry) pieces_.invalidate();
    operation_ = Operation::Idle;
    result_ = Result::Canceled;
    invalidate();
}

void BoardController::fail(const __FlashStringHelper *message) {
    cancel();
    result_ = Result::Fault;
    Serial.println(message);
    Serial.println(F("Board canceled; magnet OFF. Use HOME before board moves."));
}

void BoardController::finish() {
    magnet_.off(); restoreProfiles();
    pieces_.complete(source_, destination_);
    Serial.println(F("Piece motion complete; reference retained."));
    operation_ = Operation::Idle;
    result_ = Result::Complete;
}

void BoardController::update() {
    if (!isActive()) return;
    // Sample both limit inputs only during a moving segment; idle and magnet
    // settle stages cannot trigger a board stop.
    if (stage_ == Stage::Moving &&
        (axes_[0]->isRunning() || axes_[1]->isRunning()) &&
        (axes_[0]->boardLimitTriggered() || axes_[1]->boardLimitTriggered())) {
        fail(F("Board limit hit; both axes stopped.")); return;
    }
    const uint32_t elapsed = millis() - stageStartedMs_;
    if (stage_ == Stage::TravelRelease) {
        if (elapsed < Config::MAGNET_SETTLE_MS) return;
        moveRoutePoint();
        return;
    }
    if (stage_ == Stage::Moving) {
        if (elapsed >= Config::BOARD_MOVE_TIMEOUT_MS) { fail(F("Board move timed out.")); return; }
        if (axes_[0]->isRunning() || axes_[1]->isRunning()) return;
        if (axes_[0]->positionPulses() != targets_[0] || axes_[1]->positionPulses() != targets_[1]) {
            fail(F("Board destination not reached.")); return;
        }
        Serial.print(F("Waypoint ")); Serial.print(routeIndex_); Serial.print('/'); Serial.println(routeCount_ - 1);
        stageStartedMs_ = millis();
        // Pickup and release each wait for the relay/magnet to settle before
        // moving or reporting DONE; intermediate waypoints use a short dwell.
        if (routeIndex_ == 0) {
            magnet_.on(); stage_ = Stage::Pickup;
        } else if (routeIndex_ + 1 == routeCount_) {
            magnet_.off(); stage_ = Stage::Release;
        } else stage_ = Stage::Dwell;
        return;
    }
    const uint16_t waitMs = stage_ == Stage::Dwell ? Config::BOARD_DWELL_MS : Config::MAGNET_SETTLE_MS;
    if (elapsed < waitMs) return;
    if (stage_ == Stage::Release) { finish(); return; }
    if (++routeIndex_ == routeCount_) finish();
    else moveRoutePoint();
}
