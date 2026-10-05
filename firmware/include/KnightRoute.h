#pragma once
#include <stdint.h>

// Integer half-square coordinates: a1=(1,1), h8=(15,15).
// Four points need only eight bytes. No occupancy map or search allocation.
struct HalfSquarePoint { int8_t x; int8_t y; };
inline HalfSquarePoint parkingPoint(uint8_t slot) {
    if (slot < 9) return {static_cast<int8_t>(2 * slot), 0};
    if (slot < 18) return {static_cast<int8_t>(2 * (slot - 9)), 16};
    if (slot < 25) return {0, static_cast<int8_t>(2 * (slot - 17))};
    return {16, static_cast<int8_t>(2 * (slot - 24))};
}
struct KnightRoute {
    HalfSquarePoint points[4];

    // Parking uses a direct diagonal from an adjacent edge square. No travel
    // along an occupied perimeter and no automatic relocation of blockers.
    bool buildParking(uint8_t file, uint8_t rank, uint8_t slot, bool retrieve) {
        if (file > 7 || rank > 7 || slot > 31) return false;
        const HalfSquarePoint square = {static_cast<int8_t>(2 * file + 1),
                                       static_cast<int8_t>(2 * rank + 1)};
        const HalfSquarePoint park = parkingPoint(slot);
        const int8_t dx = park.x - square.x, dy = park.y - square.y;
        if ((dx != 1 && dx != -1) || (dy != 1 && dy != -1)) return false;
        points[retrieve ? 1 : 0] = square;
        points[retrieve ? 0 : 1] = park;
        return true;
    }

    bool build(uint8_t fromFile, uint8_t fromRank, uint8_t toFile, uint8_t toRank) {
        if (fromFile > 7 || fromRank > 7 || toFile > 7 || toRank > 7) return false;
        const int8_t dx = static_cast<int8_t>(toFile) - fromFile;
        const int8_t dy = static_cast<int8_t>(toRank) - fromRank;
        const uint8_t ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
        if (!((ax == 1 && ay == 2) || (ax == 2 && ay == 1))) return false;
        const int8_t sx = dx > 0 ? 1 : -1, sy = dy > 0 ? 1 : -1;
        points[0] = {static_cast<int8_t>(2 * fromFile + 1), static_cast<int8_t>(2 * fromRank + 1)};
        points[3] = {static_cast<int8_t>(2 * toFile + 1), static_cast<int8_t>(2 * toRank + 1)};
        // Diagonal half-square entry/exit; one square along a grid boundary.
        points[1] = {static_cast<int8_t>(points[0].x + sx), static_cast<int8_t>(points[0].y + sy)};
        points[2] = {static_cast<int8_t>(points[3].x - sx), static_cast<int8_t>(points[3].y - sy)};
        return true;
    }
};
