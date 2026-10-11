#include "BoardController.h"

// Route validation and location-to-table coordinate conversion.

namespace {
// Square IDs are rank*8+file; parking IDs are 64+slot.
HalfSquarePoint locationHalf(uint8_t id) {
    if (id >= 64) return parkingPoint(id - 64);
    return {static_cast<int8_t>(2 * (id % 8) + 1),
            static_cast<int8_t>(2 * (id / 8) + 1)};
}

bool pointOnSegment(HalfSquarePoint p, HalfSquarePoint a, HalfSquarePoint b) {
    const int16_t dx = b.x - a.x, dy = b.y - a.y;
    const int16_t px = p.x - a.x, py = p.y - a.y;
    const int16_t dot = px * dx + py * dy;
    return px * dy == py * dx && dot >= 0 && dot <= dx * dx + dy * dy;
}

float distanceToSegmentSquared(BoardPoint p, BoardPoint a, BoardPoint b) {
    const float dx = b.x - a.x, dy = b.y - a.y;
    const float lengthSquared = dx * dx + dy * dy;
    const float along = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSquared;
    const float t = fmaxf(0.0f, fminf(1.0f, along));
    const float x = p.x - a.x - t * dx, y = p.y - a.y - t * dy;
    return x * x + y * y;
}
}

BoardController::StartError BoardController::prepareReleaseTarget(
    BoardPoint previous, BoardPoint nominal, uint8_t movingId, BoardPoint &release) const {
    const float dx = nominal.x - previous.x, dy = nominal.y - previous.y;
    const float length = sqrtf(dx * dx + dy * dy);
    if (length <= 0.0f) return StartError::Route;
    float scale = Config::DELIVERY_OVERSHOOT_MM / length;
    for (uint8_t attempt = 0; attempt < 2; ++attempt) {
        release = {nominal.x + dx * scale, nominal.y + dy * scale};
        if (inside(0, lroundf(release.x * Config::PULSES_PER_MM)) &&
            inside(1, lroundf(release.y * Config::PULSES_PER_MM))) {
            const float requiredSquared = Config::PIECE_DIAMETER_MM * Config::PIECE_DIAMETER_MM;
            for (uint8_t occupied = 0; occupied < 96; ++occupied) {
                if (occupied == movingId || !pieces_.occupied(occupied)) continue;
                if (distanceToSegmentSquared(routePoint(locationHalf(occupied)), nominal, release) <
                    requiredSquared) return StartError::Route;
            }
            return StartError::None;
        }
        scale *= 0.90f;
    }
    return StartError::Geometry;
}

BoardController::StartError BoardController::startCarry(
    uint8_t source, uint8_t destination, const HalfSquarePoint *points, uint8_t count) {
    // Validate everything while still stationary; a rejected command must not
    // approach its source or energize the magnet.
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) return StartError::Busy;
    if (!referenced()) return StartError::Reference;
    if (!pieces_.canMove(source, destination)) return StartError::Map;
    if (source == destination || (source >= 64 && destination >= 64) ||
        points == nullptr || count < 2 || count > MAX_WAYPOINTS) return StartError::Route;
    const HalfSquarePoint sourcePoint = locationHalf(source), destinationPoint = locationHalf(destination);
    if (points[0].x != sourcePoint.x || points[0].y != sourcePoint.y ||
        points[count - 1].x != destinationPoint.x || points[count - 1].y != destinationPoint.y)
        return StartError::Route;
    const uint8_t parking = source >= 64 ? source : destination;
    const bool hasParking = parking >= 64;
    const HalfSquarePoint parkPoint = hasParking ? locationHalf(parking) : HalfSquarePoint{0, 0};
    for (uint8_t i = 0; i < count; ++i) {
        const HalfSquarePoint p = points[i];
        // Odd/odd points are square centers; even/even points are grid intersections
        // and parking slots. Only a parking approach may visit an empty
        // adjacent square center between its source and destination.
        if (p.x < 0 || p.x > 16 || p.y < 0 || p.y > 16 ||
            (p.x & 1) != (p.y & 1)) return StartError::Route;
        const bool square = (p.x & 1) != 0;
        if (i > 0 && i + 1 < count && square) {
            const uint8_t squareId = ((p.y - 1) / 2) * 8 + (p.x - 1) / 2;
            const int8_t dx = p.x - parkPoint.x, dy = p.y - parkPoint.y;
            if (!hasParking || (dx != 1 && dx != -1) ||
                (dy != 1 && dy != -1) || pieces_.occupied(squareId)) return StartError::Route;
        }
        const BoardPoint target = routePoint(p);
        if (!inside(0, lroundf(target.x * Config::PULSES_PER_MM)) ||
            !inside(1, lroundf(target.y * Config::PULSES_PER_MM))) return StartError::Geometry;
        if (i == 0) continue;
        const HalfSquarePoint previous = points[i - 1];
        const int8_t dx = p.x - previous.x, dy = p.y - previous.y;
        // Even/even legs follow the grid (including 45-degree diagonals);
        // odd/odd to odd/odd is forbidden. A parity change is one diagonal
        // step into or out of a square center/parking approach.
        if (square == ((previous.x & 1) != 0)) {
            // A 45-degree grid diagonal may pass through an empty square center.
            // The occupied-center check below still rejects a standing piece.
            if (square || !((dx == 0 && dy != 0) || (dy == 0 && dx != 0) ||
                            (dx != 0 && abs(dx) == abs(dy))))
                return StartError::Route;
        } else if ((dx != 1 && dx != -1) || (dy != 1 && dy != -1))
            return StartError::Route;
        // The host plans a path around occupied centers. Verify that an
        // occupied square or parking slot is not crossed by any segment.
        for (uint8_t occupied = 0; occupied < 96; ++occupied) {
            if (occupied != source && pieces_.occupied(occupied) &&
                pointOnSegment(locationHalf(occupied), previous, p)) return StartError::Route;
        }
    }
    BoardPoint release = routePoint(points[count - 1]);
    if (destination < 64) {
        const StartError releaseError = prepareReleaseTarget(
            routePoint(points[count - 2]), release, source, release);
        if (releaseError != StartError::None) return releaseError;
    }
    if (!preflight()) return StartError::Geometry;
    for (uint8_t i = 0; i < count; ++i) route_[i] = points[i];
    routeCount_ = count;
    routeIndex_ = 0;
    source_ = source; destination_ = destination;
    releaseTarget_ = release;
    operation_ = Operation::Carry;
    result_ = Result::None;
    waitForRelease();
    return StartError::None;
}

BoardController::StartError BoardController::startCarryOffset(
    uint8_t source, int8_t dx, int8_t dy, uint8_t destination,
    const HalfSquarePoint *points, uint8_t count) {
    // Keep the source center as the first *encoded* waypoint, so the normal
    // CARRY validator still checks topology and occupancy. The actual pickup
    // and first loaded leg start at the previously commanded offset corner.
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) return StartError::Busy;
    if (!referenced()) return StartError::Reference;
    if (source >= 64 || destination >= 64 || source == destination ||
        (dx != -1 && dx != 1) || (dy != -1 && dy != 1) ||
        points == nullptr || count < 2 || count > MAX_WAYPOINTS) return StartError::Route;
    if (!pieces_.canMove(source, destination)) return StartError::Map;
    const HalfSquarePoint sourcePoint = locationHalf(source);
    if (points[0].x != sourcePoint.x || points[0].y != sourcePoint.y)
        return StartError::Route;

    const BoardPoint center = routePoint(sourcePoint);
    const float inset = 0.30f * Config::BOARD_SQUARE_MM;
    const BoardPoint corner = {center.x + dx * inset, center.y + dy * inset};
    if (!inside(0, lroundf(corner.x * Config::PULSES_PER_MM)) ||
        !inside(1, lroundf(corner.y * Config::PULSES_PER_MM))) return StartError::Geometry;
    const BoardPoint firstGrid = routePoint(points[1]);
    if (!inside(0, lroundf(firstGrid.x * Config::PULSES_PER_MM)) ||
        !inside(1, lroundf(firstGrid.y * Config::PULSES_PER_MM))) return StartError::Geometry;
    const float requiredSquared = Config::PIECE_DIAMETER_MM * Config::PIECE_DIAMETER_MM;
    for (uint8_t occupied = 0; occupied < 96; ++occupied) {
        if (occupied == source || !pieces_.occupied(occupied)) continue;
        const BoardPoint standing = routePoint(locationHalf(occupied));
        if (distanceToSegmentSquared(standing, corner, firstGrid) < requiredSquared)
            return StartError::Route;
    }

    // Every valid board-to-board route has a grid waypoint before delivery,
    // so startCarry computes its final loaded vector from the same waypoint.
    const StartError result = startCarry(source, destination, points, count);
    if (result != StartError::None) return result;
    offsetStart_ = corner;
    operation_ = Operation::CarryOffset;
    return StartError::None;
}

BoardController::StartError BoardController::startOffset(
    uint8_t square, int8_t dx, int8_t dy, bool out) {
    // OFFSET never changes MAP: the piece remains assigned to its own square.
    // Validate the short loaded path before traveling to the pickup point.
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) return StartError::Busy;
    if (!referenced()) return StartError::Reference;
    if (!pieces_.known() || square >= 64 || !pieces_.occupied(square)) return StartError::Map;
    if ((dx != -1 && dx != 1) || (dy != -1 && dy != 1)) return StartError::Route;

    const BoardPoint center = routePoint(locationHalf(square));
    // 30% of one full square pitch on each axis: distinctly inside the square,
    // never at a grid intersection or perimeter parking-slot center.
    const float inset = 0.30f * Config::BOARD_SQUARE_MM;
    const BoardPoint corner = {center.x + dx * inset, center.y + dy * inset};
    if (!inside(0, lroundf(corner.x * Config::PULSES_PER_MM)) ||
        !inside(1, lroundf(corner.y * Config::PULSES_PER_MM))) return StartError::Geometry;
    for (uint8_t occupied = 0; occupied < 96; ++occupied) {
        if (occupied == square || !pieces_.occupied(occupied)) continue;
        const BoardPoint standing = routePoint(locationHalf(occupied));
        const float required = Config::PIECE_DIAMETER_MM;
        if (distanceToSegmentSquared(standing, center, corner) < required * required)
            return StartError::Route;
    }
    const BoardPoint start = out ? center : corner;
    const BoardPoint end = out ? corner : center;
    BoardPoint release;
    const StartError releaseError = prepareReleaseTarget(start, end, square, release);
    if (releaseError != StartError::None) return releaseError;
    if (!preflight()) return StartError::Geometry;
    offsetStart_ = start;
    releaseTarget_ = release;
    source_ = square; destination_ = square;
    routeCount_ = 2;
    routeIndex_ = 0;
    operation_ = Operation::Offset;
    result_ = Result::None;
    waitForRelease();
    return StartError::None;
}

BoardPoint BoardController::routePoint(HalfSquarePoint point) const {
    // The host sends integer half-square positions; convert them using the
    // measured board origin rather than duplicating geometry in the protocol.
    BoardPoint result = {size_.boardOriginXmm + point.x * Config::BOARD_SQUARE_MM / 2,
                         size_.boardOriginYmm + point.y * Config::BOARD_SQUARE_MM / 2};
    float *values[] = {&result.x, &result.y};
    const int8_t halves[] = {point.x, point.y};
    const float spans[] = {size_.usableXmm + 2 * Config::END_CLEARANCE_MM,
                           size_.usableYmm + 2 * Config::END_CLEARANCE_MM};
    // Only outer parking coordinates are clamped. Round INWARD to full pulses
    // so a rounded endpoint never lies beyond the measured software inset.
    for (uint8_t i = 0; i < 2; ++i) {
        if (halves[i] == 0) *values[i] = fmaxf(*values[i],
            ceilf(Config::END_CLEARANCE_MM * Config::PULSES_PER_MM) / Config::PULSES_PER_MM);
        if (halves[i] == 16) *values[i] = fminf(*values[i],
            floorf((spans[i] - Config::END_CLEARANCE_MM) * Config::PULSES_PER_MM) / Config::PULSES_PER_MM);
    }
    return result;
}

bool BoardController::crowdedHorizontalSegment() const {
    if ((operation_ != Operation::Carry && operation_ != Operation::CarryOffset) ||
        routeIndex_ == 0) return false;
    const HalfSquarePoint a = route_[routeIndex_ - 1], b = route_[routeIndex_];
    // Only magnet-on grid-to-grid horizontal travel uses this slower profile.
    // Neighboring square centers one half-pitch above or below the lane can
    // touch an 18 mm moving piece even though the route crosses no center.
    if (a.y != b.y || a.x == b.x || (a.x & 1) || (b.x & 1) || (a.y & 1)) return false;
    const int8_t left = a.x < b.x ? a.x : b.x;
    const int8_t right = a.x > b.x ? a.x : b.x;
    for (int8_t x = left + 1; x < right; x += 2) {
        const uint8_t file = (x - 1) / 2;
        if (a.y > 0 && pieces_.occupied(((a.y - 2) / 2) * 8 + file)) return true;
        if (a.y < 16 && pieces_.occupied((a.y / 2) * 8 + file)) return true;
    }
    return false;
}
