#pragma once
#include "AxisCalibration.h"
#include "Electromagnet.h"
#include "TableGeometry.h"
#include "BoardCoordinates.h"
#include "BoardStorage.h"
#include "PieceMap.h"

// A nonblocking CARRY owns both axes and the magnet until completion.
// Occupancy describes commanded centers; no physical piece sensors are present.
class BoardController {
public:
    enum class Result : uint8_t { None, Complete, Canceled, Fault };
    enum class StartError : uint8_t { None, Busy, Reference, Map, Route, Geometry };
    static constexpr uint8_t MAX_WAYPOINTS = 12;
    BoardController(LinearAxis &x, LinearAxis &y, AxisCalibration &xc,
                    AxisCalibration &yc, Electromagnet &magnet);

    // Geometry survives a reboot in EEPROM; the motor reference and map do not.
    bool isActive() const { return operation_ != Operation::Idle; }
    bool generate(bool useSaved = false);
    void loadSaved();
    void save();
    bool hasSavedGeometry() const { return hasSaved_; }
    float savedSpan(uint8_t axis) const { return saved_.span[axis]; }
    bool referenced() const;
    float boardOriginXmm() const { return size_.boardOriginXmm; }
    float boardOriginYmm() const { return size_.boardOriginYmm; }
    bool mapKnown() const { return pieces_.known(); }
    bool loadMap(const uint8_t bytes[12]);
    // Validate the entire commanded route before starting any motor or magnet.
    StartError startCarry(uint8_t source, uint8_t destination,
                          const HalfSquarePoint *points, uint8_t count);
    // Completion is a one-shot event consumed by the framed serial protocol.
    Result takeResult() { const Result result = result_; result_ = Result::None; return result; }

    void invalidate();
    // Called on every loop iteration; advances motion and watches moving limits.
    void update();
    void cancel(); // Stops both; drops magnet and invalidates references.

private:
    enum class Operation : uint8_t { Idle, Carry };
    // Each route point has one moving stage and a timed magnet/dwell stage.
    // TravelRelease precedes the initial fast trip to the source.
    enum class Stage : uint8_t { TravelRelease, Moving, Dwell, Pickup, Release };
    void waitForRelease();
    bool preflight();
    bool inside(uint8_t axis, int32_t pulses) const;
    bool moveTo(BoardPoint point, bool pieceProfile, bool tightHorizontal = false);
    void moveRoutePoint();
    bool crowdedHorizontalSegment() const;
    BoardPoint routePoint(HalfSquarePoint point) const;
    void restoreProfiles();
    void finish();
    void fail(const __FlashStringHelper *message);
    LinearAxis *axes_[2];
    AxisCalibration *calibrations_[2];
    Electromagnet &magnet_;
    TableSize size_;
    BoardGeometry saved_ = {};
    bool hasSaved_ = false;
    PieceMap pieces_;
    bool generated_ = false;
    Result result_ = Result::None;
    Operation operation_ = Operation::Idle;
    Stage stage_ = Stage::Moving;
    HalfSquarePoint route_[MAX_WAYPOINTS] = {};
    uint8_t routeIndex_ = 0;
    uint8_t routeCount_ = 0;
    uint8_t source_ = 0, destination_ = 0;
    uint32_t stageStartedMs_ = 0;
    float savedSpeed_[2] = {};
    int32_t targets_[2] = {};
};
