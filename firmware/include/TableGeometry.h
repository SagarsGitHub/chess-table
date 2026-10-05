#pragma once
#include <math.h>
#include "config.h"

struct TableSize {
    bool valid = false;
    float usableXmm = 0;
    float usableYmm = 0;
    bool squareCentersFit = false;
    float boardOriginXmm = 0; // Printed board edge relative to MIN trip point.
    float boardOriginYmm = 0;
};

// Measurements describe magnet-center travel, so do not subtract stage width.
// This evaluates reach/size only; it cannot certify collision-free piece routes.
inline TableSize calculateUsableTableSize(float xTravelMm, float yTravelMm) {
    TableSize result;
    const float borders = 2.0f * Config::END_CLEARANCE_MM;
    if (!isfinite(xTravelMm) || !isfinite(yTravelMm) ||
        xTravelMm <= borders || yTravelMm <= borders) return result;
    result.valid = true;
    result.usableXmm = xTravelMm - borders;
    result.usableYmm = yTravelMm - borders;
    const float plannedBoardMm = 8.0f * Config::BOARD_SQUARE_MM;
    // Keep the tested pitch even when the outer printed grid lines are outside
    // magnet travel. Only the 7*pitch span between square CENTERS must fit.
    result.squareCentersFit = 7.0f * Config::BOARD_SQUARE_MM <= fminf(result.usableXmm, result.usableYmm);
    result.boardOriginXmm = (xTravelMm - plannedBoardMm) / 2.0f;
    result.boardOriginYmm = (yTravelMm - plannedBoardMm) / 2.0f;
    return result;
}

struct BoardPoint { float x; float y; };

inline BoardPoint squareCenter(const TableSize &size, uint8_t file, uint8_t rank) {
    return {size.boardOriginXmm + (file + 0.5f) * Config::BOARD_SQUARE_MM,
            size.boardOriginYmm + (rank + 0.5f) * Config::BOARD_SQUARE_MM};
}
