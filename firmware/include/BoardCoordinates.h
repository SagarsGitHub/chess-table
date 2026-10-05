#pragma once
#include <stdint.h>

// Wire-route coordinates: square centers are odd/odd; perimeter slots are even/even.
// Keep this mapping in sync with computer/chess_table_host/routing.py.
struct HalfSquarePoint { int8_t x; int8_t y; };
inline HalfSquarePoint parkingPoint(uint8_t slot) {
    if (slot < 9) return {static_cast<int8_t>(2 * slot), 0};
    if (slot < 18) return {static_cast<int8_t>(2 * (slot - 9)), 16};
    if (slot < 25) return {0, static_cast<int8_t>(2 * (slot - 17))};
    return {16, static_cast<int8_t>(2 * (slot - 24))};
}
