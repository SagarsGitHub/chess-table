#include "LinearAxis.h"
#include "config.h"
#include <math.h>
#include <util/atomic.h>

LinearAxis::LinearAxis(uint8_t stepPin, uint8_t directionPin,
                       uint8_t minLimitPin, uint8_t maxLimitPin,
                       bool minLimitEnabled, bool maxLimitEnabled, uint8_t outwardLevel)
    : stepPin_(stepPin), directionPin_(directionPin), minLimitPin_(minLimitPin),
      maxLimitPin_(maxLimitPin), minLimitEnabled_(minLimitEnabled),
      maxLimitEnabled_(maxLimitEnabled), outwardLevel_(outwardLevel) {}

void LinearAxis::initializePins() {
    // Start LOW without producing a spurious rising STEP edge.
    digitalWrite(stepPin_, LOW);
    pinMode(stepPin_, OUTPUT);
    digitalWrite(directionPin_, outwardLevel_);
    pinMode(directionPin_, OUTPUT);
    pinMode(minLimitPin_, INPUT_PULLUP);
    pinMode(maxLimitPin_, INPUT_PULLUP);
}

bool LinearAxis::begin(FastAccelStepper *motor) {
    motor_ = motor;
    ready_ = false;
    if (motor_ == nullptr) return false;
    motor_->setDirectionPin(directionPin_, outwardLevel_ == HIGH, Config::DIR_SETUP_US);
    // ENA is unconnected; automatic enable control stays off.
    ready_ = motor_->setAcceleration(Config::ACCELERATION_PULSES_PER_SEC2) == 0 &&
             setSpeedMmPerSecond(Config::MAX_SPEED_MM_PER_SEC);
    return ready_;
}

uint32_t LinearAxis::maxPulseRate() const {
    if (motor_ == nullptr) return 0;
    const uint32_t libraryLimit = motor_->getMaxSpeedInHz();
    return libraryLimit < Config::MAX_ALLOWED_PULSES_PER_SEC
               ? libraryLimit : Config::MAX_ALLOWED_PULSES_PER_SEC;
}

bool LinearAxis::setSpeedMmPerSecond(float mmPerSecond) {
    if (motor_ == nullptr || !isfinite(mmPerSecond) || mmPerSecond <= 0.0f) return false;
    const float rate = mmPerSecond * Config::PULSES_PER_MM;
    if (!isfinite(rate) || rate < 1.0f || rate > maxPulseRate()) return false;
    const uint32_t pulseRate = static_cast<uint32_t>(rate + 0.5f);
    if (motor_->setSpeedInHz(pulseRate) != 0) return false;
    speedMmPerSecond_ = mmPerSecond;
    if (motor_->isRunning()) motor_->applySpeedAcceleration();
    return true;
}

bool LinearAxis::isRunning() const {
    return motor_ != nullptr && motor_->isRunning();
}

bool LinearAxis::setAccelerationMmPerSecond2(float mmPerSecond2) {
    const float rate = mmPerSecond2 * Config::PULSES_PER_MM;
    if (motor_ == nullptr || isRunning() || !isfinite(rate) ||
        rate < 1.0f || rate >= 2147483648.0f) return false;
    return motor_->setAcceleration(static_cast<int32_t>(rate + 0.5f)) == 0;
}

bool LinearAxis::minLimitTriggered() const {
    return minLimitEnabled_ && digitalRead(minLimitPin_) == Config::LIMIT_TRIGGERED_LEVEL;
}

bool LinearAxis::maxLimitTriggered() const {
    return maxLimitEnabled_ && digitalRead(maxLimitPin_) == Config::LIMIT_TRIGGERED_LEVEL;
}

bool LinearAxis::anyLimitTriggered() const {
    return minLimitTriggered() || maxLimitTriggered();
}

bool LinearAxis::directionBlocked(int32_t pulses) const {
    return pulses > 0 ? maxLimitTriggered() : pulses < 0 && minLimitTriggered();
}

bool LinearAxis::movePulses(int32_t pulses) {
    if (!ready_ || isRunning() || directionBlocked(pulses) || pulses == 0) return false;
    const int64_t target = static_cast<int64_t>(positionPulses()) + pulses;
    if (target > INT32_MAX || target < INT32_MIN) return false;
    if (motor_->move(pulses) == MOVE_OK) return true;
    stop();
    return false;
}

bool LinearAxis::moveMm(float mm) {
    const float magnitude = fabsf(mm * Config::PULSES_PER_MM);
    // AVR float cannot represent INT32_MAX exactly: reject the 2^31 boundary.
    if (!isfinite(magnitude) || magnitude < 0.5f || magnitude >= 2147483648.0f) return false;
    const int32_t pulses = static_cast<int32_t>(magnitude + 0.5f);
    return movePulses(mm < 0.0f ? -pulses : pulses);
}

void LinearAxis::stop() {
    // forceStop() alone would finish up to ~20 ms of already queued pulses.
    // Preserve the pulse estimate for measuring switch-to-switch travel. The
    // AVR library restores interrupt state, so its critical sections nest here.
    if (motor_ != nullptr) {
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            const int32_t position = motor_->getCurrentPosition();
            motor_->forceStopAndNewPosition(position);
        }
    }
}

int32_t LinearAxis::positionPulses() const {
    return motor_ == nullptr ? 0 : motor_->getCurrentPosition();
}

bool LinearAxis::setPositionPulses(int32_t position) {
    if (!ready_ || isRunning()) return false;
    motor_->setCurrentPosition(position);
    return true;
}
