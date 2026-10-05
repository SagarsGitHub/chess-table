#pragma once
#include <stdint.h>

// 0..63: rank*8+file; 64..95: perimeter slots 00..31.
class PieceMap {
public:
    bool known() const { return known_; }
    void invalidate() { known_ = false; }
    bool occupied(uint8_t p) const { return p < 96 && (bits_[p / 8] & (1U << (p % 8))); }
    void set(uint8_t p, bool present) {
        if (p >= 96) return;
        if (present) bits_[p / 8] |= 1U << (p % 8);
        else bits_[p / 8] &= ~(1U << (p % 8));
    }
    void reset(bool startingPosition) {
        for (uint8_t i = 0; i < 12; ++i)
            bits_[i] = startingPosition && (i < 2 || i == 6 || i == 7) ? 0xFF : 0;
        known_ = true;
    }
    void load(const uint8_t bytes[12]) {
        for (uint8_t i = 0; i < 12; ++i) bits_[i] = bytes[i];
        known_ = true;
    }
    bool canMove(uint8_t from, uint8_t to) const {
        return known_ && from < 96 && to < 96 && occupied(from) && !occupied(to);
    }
    void complete(uint8_t from, uint8_t to) { set(from, false); set(to, true); }
private:
    uint8_t bits_[12] = {};
    bool known_ = false;
};
