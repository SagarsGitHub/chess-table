#pragma once
#include "AxisCalibration.h"
#include "Electromagnet.h"
#include "TableGeometry.h"
#include "KnightRoute.h"
#include "BoardStorage.h"
#include "PieceMap.h"

// Nonblocking board operations own both axes and the magnet until completion.
// Occupancy describes commanded centers; no physical piece sensors are present.
class BoardController {
public:
    BoardController(LinearAxis &x, LinearAxis &y, AxisCalibration &xc,
                    AxisCalibration &yc, Electromagnet &magnet);
    bool isActive() const { return operation_ != Operation::Idle; }
    bool generate(bool useSaved = false);
    void loadSaved();
    void save();
    bool hasSavedGeometry() const { return hasSaved_; }
    float savedSpan(uint8_t axis) const { return saved_.span[axis]; }
    bool referenced() const;
    void printState();
    void resetPieces(bool full);
    void editPiece(uint8_t location, bool present);
    void printPieces();
    void nextFixture();
    void invalidate();
    void printGrid();
    void startSquare(uint8_t file, uint8_t rank);
    void startWalk(bool allSquares);
    void startKnight(uint8_t fromFile, uint8_t fromRank, uint8_t toFile, uint8_t toRank, bool carryPiece);
    void startParking(uint8_t file, uint8_t rank, uint8_t slot, bool retrieve);
    void update();
    void cancel(); // Stops both; drops magnet and invalidates references.

private:
    enum class Operation : uint8_t { Idle, Square, Next, Walk, DryKnight, CarryKnight };
    enum class Stage : uint8_t { TravelRelease, Moving, Dwell, Pickup, Release };
    void waitForRelease();
    bool preflight();
    bool inside(uint8_t axis, int32_t pulses) const;
    bool moveTo(BoardPoint point, bool pieceProfile);
    void moveSquare(uint8_t file, uint8_t rank);
    void moveWalkSquare();
    void moveKnightPoint();
    BoardPoint knightPoint(uint8_t index) const;
    BoardPoint routePoint(HalfSquarePoint point) const;
    void startRoute(bool carry, uint8_t source, uint8_t destination);
    void restoreProfiles();
    void finish();
    void fail(const __FlashStringHelper *message);
    void printSquare(uint8_t file, uint8_t rank);
    LinearAxis *axes_[2];
    AxisCalibration *calibrations_[2];
    Electromagnet &magnet_;
    TableSize size_;
    BoardGeometry saved_ = {};
    bool hasSaved_ = false;
    PieceMap pieces_;
    uint8_t fixtureStep_ = 255;
    bool fixtureMove_ = false;
    bool generated_ = false;
    Operation operation_ = Operation::Idle;
    Stage stage_ = Stage::Moving;
    uint8_t nextSquare_ = 0;
    uint8_t walkIndex_ = 0;
    KnightRoute route_;
    uint8_t routeIndex_ = 0;
    uint8_t routeCount_ = 4;
    uint8_t source_ = 0, destination_ = 0;
    uint8_t targetFile_ = 0;
    uint8_t targetRank_ = 0;
    uint32_t stageStartedMs_ = 0;
    float savedSpeed_[2] = {};
    int32_t targets_[2] = {};
};
