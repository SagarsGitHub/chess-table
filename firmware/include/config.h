#pragma once

#include <Arduino.h>

namespace Config {
constexpr uint8_t ELECTROMAGNET_RELAY_PIN = 2; // Relay IN: HIGH=on, LOW=off.
constexpr uint8_t X_STEP_PIN = 9;  // Uno Timer1 OC1A -> first M415B PUL.
constexpr uint8_t X_DIR_PIN = 3;
constexpr uint8_t X_MIN_LIMIT_PIN = 4; // Bare switches: NO -> input, COM -> GND.
constexpr uint8_t X_MAX_LIMIT_PIN = 7;
constexpr uint8_t Y_STEP_PIN = 10; // Uno Timer1 OC1B -> second M415B PUL.
constexpr uint8_t Y_DIR_PIN = 5;
constexpr uint8_t Y_MIN_LIMIT_PIN = 6;
constexpr uint8_t Y_MAX_LIMIT_PIN = 8;

// Normally-open (NO) switches: INPUT_PULLUP reads HIGH when released and LOW
// when pressed (closed to GND). An unplugged/broken wire also reads released.
constexpr uint8_t LIMIT_TRIGGERED_LEVEL = LOW;
// Enable only after connecting the corresponding switch to the Uno.
constexpr bool X_MIN_LIMIT_ENABLED = true;
constexpr bool X_MAX_LIMIT_ENABLED = true;
constexpr bool Y_MIN_LIMIT_ENABLED = true;
constexpr bool Y_MAX_LIMIT_ENABLED = true;
// Positive moves approach MAX; negative moves approach MIN. Set direction
// levels to match those physical ends before enabling the switches.
constexpr uint8_t X_OUTWARD_DIR_LEVEL = HIGH; // LOW reverses this axis.
constexpr uint8_t Y_OUTWARD_DIR_LEVEL = HIGH;

// MICROSTEPS must match the physical driver DIP switches.
constexpr uint16_t FULL_STEPS_PER_REV = 200; // 1.8-degree motor.
constexpr uint8_t MICROSTEPS = 8;
constexpr float SCREW_LEAD_MM = 12.0f;       // Both actuators: TR6x12, 12 mm/rev.
constexpr float PULSES_PER_MM = FULL_STEPS_PER_REV * MICROSTEPS / SCREW_LEAD_MM;

// Changing screw lead preserves the requested distances and speeds in mm.
constexpr float MAX_SPEED_MM_PER_SEC = 150.0f;
// Board profiles use the configured operating maximum, not the pulse ceiling.
constexpr float TRAVEL_SPEED_MM_PER_SEC = MAX_SPEED_MM_PER_SEC;
constexpr float PIECE_SPEED_MM_PER_SEC = 75.0f; // Requested carrying speed.
constexpr float ACCELERATION_MM_PER_SEC2 = 1000.0f;
constexpr float PIECE_ACCELERATION_MM_PER_SEC2 = ACCELERATION_MM_PER_SEC2 * (2.0f / 3.0f);
constexpr uint16_t BOARD_DWELL_MS = 500;
constexpr uint16_t MAGNET_SETTLE_MS = 350;
constexpr uint32_t BOARD_MOVE_TIMEOUT_MS = 15000;
constexpr uint32_t MAX_PULSES_PER_SEC =
    static_cast<uint32_t>(MAX_SPEED_MM_PER_SEC * PULSES_PER_MM + 0.5f);
constexpr int32_t ACCELERATION_PULSES_PER_SEC2 =
    static_cast<int32_t>(ACCELERATION_MM_PER_SEC2 * PULSES_PER_MM + 0.5f);

// Conservative dual-motor Uno ceiling. Each axis also checks the library limit.
// This is a pulse-generation ceiling, not a mechanically validated speed.
constexpr uint32_t MAX_ALLOWED_PULSES_PER_SEC = 37000;

// FastAccelStepper owns the STEP waveform: idle LOW, hardware-timed rising edge.
// AVR pulse HIGH width is handled by the library, not delayMicroseconds().
constexpr uint16_t DIR_SETUP_US = 50; // AVR library minimum is 40 us; M415B needs 5.
constexpr unsigned long SERIAL_BAUD = 115200;

// Calibration is explicit, never automatic at startup. Both end switches on
// an axis must be wired, checked, and enabled before measuring that axis.
constexpr float CALIBRATION_SEEK_MM_PER_SEC = 20.0f;
constexpr float CALIBRATION_LATCH_MM_PER_SEC = 2.0f;
constexpr float CALIBRATION_BACKOFF_MM = 3.0f;
constexpr float CALIBRATION_MAX_TRAVEL_MM = 300.0f; // Bound each long search.
constexpr float CALIBRATION_MIN_TRAVEL_MM = 20.0f;  // Reject implausibly short spans.
constexpr uint32_t CALIBRATION_STAGE_TIMEOUT_MS = 30000;
constexpr uint16_t SWITCH_SETTLE_MS = 20; // Confirm only after stopping at first edge.
constexpr uint16_t SWITCH_CONFIRM_TIMEOUT_MS = 250;
constexpr float END_CLEARANCE_MM = 2.0f; // Inset from each measured trip point.

// Latest perimeter-storage plan: 8x8 board, 32 perimeter grid intersections,
// existing 18 mm pieces. Report fit; never silently shrink the tested pitch.
// The user accepts observed neighbor displacement in these physical tests.
// Nominal circular-piece clearance is not a veto on knight/parking routes.
constexpr float BOARD_SQUARE_MM = 31.25f;
constexpr float PIECE_DIAMETER_MM = 18.0f;
} // namespace Config

static_assert(Config::SCREW_LEAD_MM > 0, "Screw lead must be positive");
static_assert(Config::FULL_STEPS_PER_REV > 0 && Config::MICROSTEPS > 0,
              "Motor step counts must be positive");
static_assert(Config::X_STEP_PIN == 9 && Config::Y_STEP_PIN == 10,
              "Dual FastAccelStepper on Uno uses D9 and D10 for STEP");
static_assert(Config::MAX_SPEED_MM_PER_SEC > 0 && Config::MAX_PULSES_PER_SEC > 0 &&
                  Config::MAX_PULSES_PER_SEC <= Config::MAX_ALLOWED_PULSES_PER_SEC,
              "Keep each axis at or below the dual-motor 37000 pulses/s ceiling");
static_assert(Config::ACCELERATION_MM_PER_SEC2 > 0 &&
                  Config::ACCELERATION_PULSES_PER_SEC2 > 0,
              "Acceleration must convert to at least one pulse/s^2");
static_assert(Config::BOARD_SQUARE_MM > 0 && Config::PIECE_DIAMETER_MM > 0,
              "Board and piece geometry must be positive");
static_assert(Config::PIECE_SPEED_MM_PER_SEC > 0 &&
                  Config::PIECE_SPEED_MM_PER_SEC <= Config::TRAVEL_SPEED_MM_PER_SEC &&
                  Config::TRAVEL_SPEED_MM_PER_SEC <= Config::MAX_SPEED_MM_PER_SEC &&
                  Config::PIECE_ACCELERATION_MM_PER_SEC2 > 0,
              "Piece profile must not exceed configured travel speed");
static_assert((Config::X_OUTWARD_DIR_LEVEL == HIGH || Config::X_OUTWARD_DIR_LEVEL == LOW) &&
                  (Config::Y_OUTWARD_DIR_LEVEL == HIGH || Config::Y_OUTWARD_DIR_LEVEL == LOW),
              "Each direction must be HIGH or LOW");
static_assert(Config::DIR_SETUP_US >= 40 && Config::DIR_SETUP_US <= 4095,
              "Use the AVR library's supported direction delay range");
static_assert(Config::LIMIT_TRIGGERED_LEVEL == LOW || Config::LIMIT_TRIGGERED_LEVEL == HIGH,
              "Limit trigger level must be LOW or HIGH");
static_assert(Config::CALIBRATION_LATCH_MM_PER_SEC > 0 &&
                  Config::CALIBRATION_SEEK_MM_PER_SEC >= Config::CALIBRATION_LATCH_MM_PER_SEC &&
                  Config::CALIBRATION_SEEK_MM_PER_SEC * Config::PULSES_PER_MM <=
                      Config::MAX_ALLOWED_PULSES_PER_SEC,
              "Calibration speeds must be positive and within the pulse ceiling");
static_assert(Config::CALIBRATION_BACKOFF_MM > 0 && Config::END_CLEARANCE_MM > 0 &&
                  Config::CALIBRATION_MIN_TRAVEL_MM > 2 * Config::END_CLEARANCE_MM &&
                  Config::CALIBRATION_MAX_TRAVEL_MM > Config::CALIBRATION_MIN_TRAVEL_MM,
              "Calibration distances and end clearance must leave usable travel");
static_assert(Config::SWITCH_CONFIRM_TIMEOUT_MS > Config::SWITCH_SETTLE_MS &&
                  Config::CALIBRATION_STAGE_TIMEOUT_MS > Config::SWITCH_CONFIRM_TIMEOUT_MS,
              "Switch settling must fit inside calibration timeouts");
