#pragma once
#include <Arduino.h>

// Drives a relay MODULE's active-HIGH logic input, not a bare relay coil.
class Electromagnet {
public:
    explicit Electromagnet(uint8_t relayPin) : pin_(relayPin) {}
    void begin() {
        digitalWrite(pin_, LOW); // Set off before switching the pin to OUTPUT.
        pinMode(pin_, OUTPUT);
        enabled_ = false;
    }
    void setEnabled(bool enabled) {
        digitalWrite(pin_, enabled ? HIGH : LOW);
        enabled_ = enabled;
    }
    void on() { setEnabled(true); }
    void off() { setEnabled(false); }
    bool isOn() const { return enabled_; }
private:
    const uint8_t pin_;
    bool enabled_ = false;
};
