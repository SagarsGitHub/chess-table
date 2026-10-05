#pragma once

#include <Arduino.h>
#include <FastAccelStepper.h>

// One actuator, with nonblocking relative motion. Call from the main loop, not
// an interrupt. BoardController/AxisCalibration own motion and limit handling.
class LinearAxis {
public:
    LinearAxis(uint8_t stepPin, uint8_t directionPin,
               uint8_t minLimitPin, uint8_t maxLimitPin,
               bool minLimitEnabled, bool maxLimitEnabled, uint8_t outwardLevel);

    void initializePins();
    // Allocate BOTH FastAccelStepper motors before calling begin() on either.
    bool begin(FastAccelStepper *motor);
    uint8_t stepPin() const { return stepPin_; }
    bool isReady() const { return ready_; }
    bool isRunning() const;
    bool minLimitEnabled() const { return minLimitEnabled_; }
    bool maxLimitEnabled() const { return maxLimitEnabled_; }
    bool minLimitTriggered() const;
    bool maxLimitTriggered() const;
    bool anyLimitTriggered() const;
    bool directionBlocked(int32_t pulses) const;
    // Board travel polls these only while a motor is moving. Calibration
    // continues to read raw limit state above.
    bool boardLimitTriggered() const;
    void resetBoardLimitFilter();

    // Returns false on invalid/out-of-range speed; previous setting is retained.
    // Applies to an ongoing move with the configured acceleration, without
    // changing its destination or direction. Does not start an idle motor.
    bool setSpeedMmPerSecond(float mmPerSecond);
    bool setAccelerationMmPerSecond2(float mmPerSecond2); // Idle only.
    float speedMmPerSecond() const { return speedMmPerSecond_; }

    // Rejects busy, uninitialized, blocked-direction, or zero-distance requests.
    // boardTravel bypasses the raw switch guard; BoardController watches limits
    // after motors start. Calibration always uses the immediate raw guard.
    bool movePulses(int32_t pulses, bool boardTravel = false);
    bool moveMm(float mm);
    // Commanded pulse position, zeroed at the MIN slow latch by calibration.
    // This is an open-loop estimate; it does not detect mechanical missed steps.
    int32_t positionPulses() const;
    bool setPositionPulses(int32_t position); // Idle only.
    void stop(); // Cancel the ramp AND queued pulses; no deceleration.

private:
    bool confirmedLimit(uint8_t pin, bool enabled, bool &seen,
                        uint32_t &sinceUs) const;
    uint32_t maxPulseRate() const;
    const uint8_t stepPin_;
    const uint8_t directionPin_;
    const uint8_t minLimitPin_;
    const uint8_t maxLimitPin_;
    const bool minLimitEnabled_;
    const bool maxLimitEnabled_;
    const uint8_t outwardLevel_;
    FastAccelStepper *motor_ = nullptr;
    bool ready_ = false;
    float speedMmPerSecond_ = 0.0f;
    mutable bool minSeen_ = false, maxSeen_ = false;
    mutable uint32_t minSinceUs_ = 0, maxSinceUs_ = 0;
};
