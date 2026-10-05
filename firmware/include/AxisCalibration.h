#pragma once
#include "LinearAxis.h"

// Nonblocking end-to-end measurement. While active, this object exclusively owns
// its axis: call update() every loop instead of normal move/limit handling.
class AxisCalibration {
public:
    explicit AxisCalibration(LinearAxis &axis) : axis_(axis) {}
    bool start(float savedSpanMm = 0); // Nonzero: MIN-only reference using saved span.
    void update();
    void cancel();
    void invalidate() { valid_ = false; }
    bool isActive() const;
    bool isCalibrated() const { return valid_; }
    float travelMm() const;
    const __FlashStringHelper *errorMessage() const { return error_; }

private:
    // Full HOME measures MIN to MAX; REF stops after the MIN latch and reuses
    // the saved span. Both finish parked inside the measured travel range.
    enum class Stage : uint8_t {
        Idle,
        ClearMax,
        SeekMin, ConfirmMin, BackoffMin, LatchMin, ConfirmMinLatch, LeaveMin,
        SeekMax, ConfirmMax, BackoffMax, LatchMax, ConfirmMaxLatch,
        Park
    };
    void move(Stage stage, float mm, float speed);
    void confirm(Stage stage);
    void fail(const __FlashStringHelper *message);
    void restoreSpeed();
    LinearAxis &axis_;
    Stage stage_ = Stage::Idle;
    uint32_t stageStartedMs_ = 0;
    uint32_t stableSinceMs_ = 0;
    bool stable_ = false;
    bool valid_ = false;
    float savedSpeed_ = 0;
    int32_t spanPulses_ = 0;
    bool referenceOnly_ = false;
    const __FlashStringHelper *error_ = nullptr;
};
