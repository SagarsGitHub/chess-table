#include "BoardController.h"
#include <util/atomic.h>
#include <avr/pgmspace.h>

namespace {
// Every pair goes out and back, so each starts with the normal 32-piece setup.
// Square IDs are rank*8+file; parking IDs are 64+slot.
const uint8_t fixture[][2] PROGMEM = {
    {1,16},{1,18},{6,21},{6,23}, {57,40},{57,42},{62,45},{62,47},
    {0,64},{7,72},{56,73},{63,81}, // four corner slots
    {8,82},{48,88},{15,89},{55,95}, // left/right pawn-edge slots
    {3,68},{59,77} // middle of bottom/top edges
};
constexpr uint8_t FIXTURE_STEPS = sizeof(fixture) / sizeof(fixture[0]) * 2;

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
}

BoardController::BoardController(LinearAxis &x, LinearAxis &y, AxisCalibration &xc,
                                 AxisCalibration &yc, Electromagnet &magnet)
    : axes_{&x, &y}, calibrations_{&xc, &yc}, magnet_(magnet) {}

void BoardController::invalidate() {
    nextSquare_ = 0;
    calibrations_[0]->invalidate();
    calibrations_[1]->invalidate();
}

bool BoardController::referenced() const {
    return generated_ && calibrations_[0]->isCalibrated() && calibrations_[1]->isCalibrated();
}

bool BoardController::loadMap(const uint8_t bytes[12]) {
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) return false;
    pieces_.load(bytes);
    return true;
}

BoardController::StartError BoardController::startCarry(
    uint8_t source, uint8_t destination, const HalfSquarePoint *points, uint8_t count) {
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
    if (!preflight()) return StartError::Geometry;
    for (uint8_t i = 0; i < count; ++i) route_[i] = points[i];
    routeCount_ = count;
    routeIndex_ = 0;
    source_ = source; destination_ = destination;
    operation_ = Operation::CarryKnight;
    result_ = Result::None;
    waitForRelease();
    return StartError::None;
}

void BoardController::loadSaved() {
    hasSaved_ = loadBoardGeometry(saved_);
    if (hasSaved_) {
        size_ = calculateUsableTableSize(saved_.span[0], saved_.span[1]);
        size_.boardOriginXmm = saved_.origin[0];
        size_.boardOriginYmm = saved_.origin[1];
        generated_ = true;
    }
    Serial.println(hasSaved_ ? F("EEPROM geometry loaded; r=MIN reference, h=remeasure.") :
                              F("No valid saved geometry; h then s."));
}

void BoardController::save() {
    if (!referenced()) { Serial.println(F("Run h or r before saving.")); return; }
    for (uint8_t i = 0; i < 2; ++i) saved_.span[i] = calibrations_[i]->travelMm();
    saved_.origin[0] = size_.boardOriginXmm; saved_.origin[1] = size_.boardOriginYmm;
    saveBoardGeometry(saved_);
    hasSaved_ = true;
    Serial.println(F("Geometry saved to EEPROM."));
}

void BoardController::printState() {
    Serial.print(referenced() ? F("Reference ready; ") : F("Reference missing; h or saved r; "));
    Serial.println(pieces_.known() ? F("piece map known.") : F("piece map unknown: f=starting, e=empty."));
}

void BoardController::resetPieces(bool full) {
    pieces_.reset(full);
    fixtureStep_ = full ? 0 : 255;
    Serial.println(full ? F("Map: 32 starting pieces, parking empty. j=next fixture leg.") :
                          F("Map: all 96 locations empty. i edits occupancy."));
}

void BoardController::editPiece(uint8_t location, bool present) {
    if (!pieces_.known()) { Serial.println(F("Set f or e before map edits.")); return; }
    pieces_.set(location, present); fixtureStep_ = 255;
    Serial.println(F("Map updated."));
}

void BoardController::printPieces() {
    if (!pieces_.known()) { printState(); return; }
    Serial.println(F("Occupancy: ranks 8..1, files a..h; 1=piece, 0=empty"));
    for (int8_t rank = 7; rank >= 0; --rank) {
        for (uint8_t file = 0; file < 8; ++file) Serial.print(pieces_.occupied(rank * 8 + file) ? '1' : '0');
        Serial.println();
    }
    Serial.print(F("Parking 00..31: "));
    for (uint8_t i = 64; i < 96; ++i) Serial.print(pieces_.occupied(i) ? '1' : '0');
    Serial.println();
}

bool BoardController::generate(bool useSaved) {
    generated_ = false;
    if (!calibrations_[0]->isCalibrated() || !calibrations_[1]->isCalibrated()) return false;
    size_ = calculateUsableTableSize(calibrations_[0]->travelMm(), calibrations_[1]->travelMm());
    if (useSaved && hasSaved_) {
        size_.boardOriginXmm = saved_.origin[0];
        size_.boardOriginYmm = saved_.origin[1];
    }
    if (!size_.valid || !size_.squareCentersFit) {
        Serial.println(F("Board centers do not fit; pitch unchanged. Board moves disabled."));
        return false;
    }
    // Also account for pulse rounding at the four extreme center coordinates.
    for (uint8_t corner = 0; corner < 2; ++corner) {
        const BoardPoint p = squareCenter(size_, corner * 7, corner * 7);
        if (!inside(0, lroundf(p.x * Config::PULSES_PER_MM)) ||
            !inside(1, lroundf(p.y * Config::PULSES_PER_MM))) return false;
    }
    generated_ = true;
    nextSquare_ = 0;
    Serial.println(F("Board ready: a1 nearest MIN/MIN; files +X, ranks +Y."));
    return true;
}

bool BoardController::inside(uint8_t axis, int32_t pulses) const {
    const int32_t low = static_cast<int32_t>(ceilf(Config::END_CLEARANCE_MM * Config::PULSES_PER_MM));
    const int32_t high = static_cast<int32_t>(floorf(
        (calibrations_[axis]->travelMm() - Config::END_CLEARANCE_MM) * Config::PULSES_PER_MM));
    return pulses >= low && pulses <= high;
}

bool BoardController::preflight() {
    if (isActive() || axes_[0]->isRunning() || axes_[1]->isRunning() ||
        calibrations_[0]->isActive() || calibrations_[1]->isActive()) {
        Serial.println(F("Board busy; wait or x.")); return false;
    }
    if (!generated_ || !calibrations_[0]->isCalibrated() || !calibrations_[1]->isCalibrated()) {
        Serial.println(F("Board unavailable; run h first.")); return false;
    }
    for (uint8_t i = 0; i < 2; ++i) {
        if (!axes_[i]->isReady() || !inside(i, axes_[i]->positionPulses())) {
            fail(F("Board position/limit invalid; run h.")); return false;
        }
    }
    for (uint8_t i = 0; i < 2; ++i) savedSpeed_[i] = axes_[i]->speedMmPerSecond();
    magnet_.off();
    return true;
}

void BoardController::printSquare(uint8_t file, uint8_t rank) {
    Serial.print(static_cast<char>('a' + file));
    Serial.print(static_cast<char>('1' + rank));
}

void BoardController::printGrid() {
    if (!generated_) {
        Serial.println(F("Board unavailable; run h first.")); return;
    }
    Serial.println(F("Square,X_mm_from_MIN,Y_mm_from_MIN"));
    for (uint8_t rank = 0; rank < 8; ++rank) for (uint8_t file = 0; file < 8; ++file) {
        const BoardPoint p = squareCenter(size_, file, rank);
        printSquare(file, rank); Serial.print(',');
        Serial.print(p.x, 3); Serial.print(','); Serial.println(p.y, 3);
    }
    for (uint8_t slot = 0; slot < 32; ++slot) {
        const BoardPoint p = routePoint(parkingPoint(slot));
        Serial.print('P'); if (slot < 10) Serial.print('0'); Serial.print(slot); Serial.print(',');
        Serial.print(p.x, 3); Serial.print(','); Serial.println(p.y, 3);
    }
}

bool BoardController::moveTo(BoardPoint p, bool pieceProfile) {
    targets_[0] = lroundf(p.x * Config::PULSES_PER_MM);
    targets_[1] = lroundf(p.y * Config::PULSES_PER_MM);
    int32_t delta[2];
    for (uint8_t i = 0; i < 2; ++i) {
        if (!inside(i, targets_[i])) {
            fail(F("Board target outside measured bounds / limit active.")); return false;
        }
        delta[i] = targets_[i] - axes_[i]->positionPulses();
    }
    const float dx = static_cast<float>(delta[0]), dy = static_cast<float>(delta[1]);
    const float length = sqrtf(dx * dx + dy * dy);
    const float speed = pieceProfile ? Config::PIECE_SPEED_MM_PER_SEC : Config::TRAVEL_SPEED_MM_PER_SEC;
    const float acceleration = pieceProfile ? Config::PIECE_ACCELERATION_MM_PER_SEC2 : Config::ACCELERATION_MM_PER_SEC2;
    for (uint8_t i = 0; i < 2; ++i) {
        if (delta[i] == 0) continue;
        const float fraction = fabsf(static_cast<float>(delta[i])) / length;
        if (!axes_[i]->setSpeedMmPerSecond(speed * fraction) ||
            !axes_[i]->setAccelerationMmPerSecond2(acceleration * fraction)) {
            fail(F("Board motion profile rejected.")); return false;
        }
    }
    Serial.print(F("Move X,Y (mm): ")); Serial.print(p.x, 3); Serial.print(','); Serial.print(p.y, 3);
    Serial.print(pieceProfile ? F("; piece mm/s=") : F("; travel mm/s="));
    Serial.println(speed, 1);
    axes_[0]->resetBoardLimitFilter(); axes_[1]->resetBoardLimitFilter();
    bool accepted = true;
    // Submit both ramps before the AVR ramp-management interrupt can run.
    // These are proportional independent ramps, not a general CNC interpolator.
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        for (uint8_t i = 0; i < 2; ++i) {
            if (delta[i] != 0 && !axes_[i]->movePulses(delta[i], true)) { accepted = false; break; }
        }
        if (!accepted) { axes_[0]->stop(); axes_[1]->stop(); }
    }
    if (!accepted) { fail(F("Board move rejected; both axes stopped.")); return false; }
    stage_ = Stage::Moving;
    stageStartedMs_ = millis();
    return true;
}

void BoardController::moveSquare(uint8_t file, uint8_t rank) {
    targetFile_ = file; targetRank_ = rank;
    moveTo(squareCenter(size_, file, rank), false);
}

void BoardController::startSquare(uint8_t file, uint8_t rank) {
    if (file > 7 || rank > 7 || !preflight()) return;
    result_ = Result::None;
    operation_ = Operation::Square;
    targetFile_ = file; targetRank_ = rank;
    waitForRelease();
}

void BoardController::waitForRelease() {
    // Allow a previously energized relay/magnet to release before fast travel.
    stage_ = Stage::TravelRelease;
    stageStartedMs_ = millis();
}

void BoardController::moveWalkSquare() {
    const uint8_t rank = walkIndex_ / 8;
    const uint8_t file = rank % 2 ? 7 - walkIndex_ % 8 : walkIndex_ % 8;
    moveSquare(file, rank);
}

void BoardController::startWalk(bool allSquares) {
    if (!preflight()) return;
    result_ = Result::None;
    operation_ = allSquares ? Operation::Walk : Operation::Next;
    walkIndex_ = allSquares ? 0 : nextSquare_;
    waitForRelease();
}

BoardPoint BoardController::knightPoint(uint8_t index) const {
    return routePoint(route_[index]);
}

BoardPoint BoardController::routePoint(HalfSquarePoint point) const {
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

void BoardController::startKnight(uint8_t fromFile, uint8_t fromRank,
                                  uint8_t toFile, uint8_t toRank, bool carryPiece) {
    // A temporary route keeps a busy command from overwriting an active route.
    KnightRoute candidate;
    if (!candidate.build(fromFile, fromRank, toFile, toRank)) {
        Serial.println(F("Invalid knight move: need 2 by 1 squares.")); return;
    }
    if (isActive()) return;
    for (uint8_t i = 0; i < 4; ++i) route_[i] = candidate.points[i];
    routeCount_ = 4;
    startRoute(carryPiece, fromRank * 8 + fromFile, toRank * 8 + toFile);
}

void BoardController::startParking(uint8_t file, uint8_t rank, uint8_t slot, bool retrieve) {
    KnightRoute candidate;
    if (!candidate.buildParking(file, rank, slot, retrieve)) {
        Serial.println(F("Invalid parking: use an adjacent edge square and slot 00..31.")); return;
    }
    if (isActive()) return;
    for (uint8_t i = 0; i < 2; ++i) route_[i] = candidate.points[i];
    routeCount_ = 2;
    const uint8_t square = rank * 8 + file, park = 64 + slot;
    startRoute(true, retrieve ? park : square, retrieve ? square : park);
}

void BoardController::startRoute(bool carryPiece, uint8_t source, uint8_t destination) {
    if (carryPiece && !pieces_.canMove(source, destination)) {
        Serial.println(F("Map rejects move: need known map, occupied source, empty destination.")); return;
    }
    if (!preflight()) return;
    // Validate every waypoint before approaching the source or energizing.
    for (uint8_t i = 0; i < routeCount_; ++i) {
        const BoardPoint p = knightPoint(i);
        if (!inside(0, lroundf(p.x * Config::PULSES_PER_MM)) ||
            !inside(1, lroundf(p.y * Config::PULSES_PER_MM))) {
            Serial.println(F("Route exceeds measured bounds.")); return;
        }
    }
    for (uint8_t i = 0; i < routeCount_; ++i) {
        const BoardPoint p = knightPoint(i);
        Serial.print(F("Route ")); Serial.print(i); Serial.print(F(": "));
        Serial.print(p.x, 3); Serial.print(','); Serial.println(p.y, 3);
    }
    operation_ = carryPiece ? Operation::CarryKnight : Operation::DryKnight;
    result_ = Result::None;
    source_ = source; destination_ = destination;
    if (carryPiece && !fixtureMove_) fixtureStep_ = 255;
    routeIndex_ = 0;
    waitForRelease();
}

void BoardController::nextFixture() {
    if (fixtureStep_ >= FIXTURE_STEPS) {
        Serial.println(F("Fixture unavailable/done. Restore starting board, then f.")); return;
    }
    const uint8_t pair = fixtureStep_ / 2;
    uint8_t a = pgm_read_byte(&fixture[pair][0]), b = pgm_read_byte(&fixture[pair][1]);
    if (fixtureStep_ % 2) { const uint8_t temp = a; a = b; b = temp; }
    Serial.print(F("Fixture leg ")); Serial.print(fixtureStep_ + 1);
    Serial.print('/'); Serial.println(FIXTURE_STEPS);
    fixtureMove_ = true;
    if (a < 64 && b < 64) startKnight(a % 8, a / 8, b % 8, b / 8, true);
    else {
        const uint8_t square = a < 64 ? a : b;
        startParking(square % 8, square / 8, (a < 64 ? b : a) - 64, a >= 64);
    }
    if (!isActive()) fixtureMove_ = false;
}

void BoardController::moveKnightPoint() {
    moveTo(knightPoint(routeIndex_), routeIndex_ != 0);
}

void BoardController::restoreProfiles() {
    for (uint8_t i = 0; i < 2; ++i) {
        axes_[i]->setAccelerationMmPerSecond2(Config::ACCELERATION_MM_PER_SEC2);
        axes_[i]->setSpeedMmPerSecond(savedSpeed_[i]);
    }
}

void BoardController::cancel() {
    axes_[0]->stop(); axes_[1]->stop(); magnet_.off();
    if (isActive()) restoreProfiles();
    if (operation_ == Operation::CarryKnight) pieces_.invalidate();
    fixtureStep_ = 255; fixtureMove_ = false;
    operation_ = Operation::Idle;
    result_ = Result::Canceled;
    invalidate();
}

void BoardController::fail(const __FlashStringHelper *message) {
    cancel();
    result_ = Result::Fault;
    Serial.println(message);
    Serial.println(F("Board canceled; magnet OFF. Run h before board moves."));
}

void BoardController::finish() {
    magnet_.off(); restoreProfiles();
    if (operation_ == Operation::CarryKnight) {
        pieces_.complete(source_, destination_);
        if (fixtureMove_) ++fixtureStep_;
    }
    Serial.println(operation_ == Operation::CarryKnight ?
        F("Piece motion complete; reference retained.") :
        F("Board motion complete."));
    fixtureMove_ = false;
    operation_ = Operation::Idle;
    result_ = Result::Complete;
}

void BoardController::update() {
    if (!isActive()) return;
    if (stage_ == Stage::Moving &&
        (axes_[0]->isRunning() || axes_[1]->isRunning()) &&
        (axes_[0]->boardLimitTriggered() || axes_[1]->boardLimitTriggered())) {
        fail(F("Board limit hit; both axes stopped.")); return;
    }
    const uint32_t elapsed = millis() - stageStartedMs_;
    if (stage_ == Stage::TravelRelease) {
        if (elapsed < Config::MAGNET_SETTLE_MS) return;
        if (operation_ == Operation::Square) moveSquare(targetFile_, targetRank_);
        else if (operation_ == Operation::Walk || operation_ == Operation::Next) moveWalkSquare();
        else moveKnightPoint();
        return;
    }
    if (stage_ == Stage::Moving) {
        if (elapsed >= Config::BOARD_MOVE_TIMEOUT_MS) { fail(F("Board move timed out.")); return; }
        if (axes_[0]->isRunning() || axes_[1]->isRunning()) return;
        if (axes_[0]->positionPulses() != targets_[0] || axes_[1]->positionPulses() != targets_[1]) {
            fail(F("Board destination not reached.")); return;
        }
        if (operation_ == Operation::CarryKnight || operation_ == Operation::DryKnight) {
            Serial.print(F("Waypoint ")); Serial.print(routeIndex_); Serial.print('/'); Serial.println(routeCount_ - 1);
        } else {
            Serial.print(F("At ")); printSquare(targetFile_, targetRank_); Serial.println();
        }
        stageStartedMs_ = millis();
        if (operation_ == Operation::Square || operation_ == Operation::Next) {
            if (operation_ == Operation::Next) nextSquare_ = (walkIndex_ + 1) % 64;
            finish(); return;
        }
        if (operation_ == Operation::CarryKnight && routeIndex_ == 0) {
            magnet_.on(); stage_ = Stage::Pickup;
        } else if (operation_ == Operation::CarryKnight && routeIndex_ + 1 == routeCount_) {
            magnet_.off(); stage_ = Stage::Release;
        } else stage_ = Stage::Dwell;
        return;
    }
    const uint16_t waitMs = stage_ == Stage::Dwell ? Config::BOARD_DWELL_MS : Config::MAGNET_SETTLE_MS;
    if (elapsed < waitMs) return;
    if (stage_ == Stage::Release) { finish(); return; }
    if (operation_ == Operation::Walk) {
        if (++walkIndex_ == 64) { nextSquare_ = 0; finish(); }
        else moveWalkSquare();
    } else {
        if (++routeIndex_ == routeCount_) finish();
        else moveKnightPoint();
    }
}
