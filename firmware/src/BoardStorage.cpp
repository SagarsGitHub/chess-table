#include "BoardStorage.h"
#include <EEPROM.h>
#include <stddef.h>

namespace {
constexpr uint8_t COMMITTED = 0xA7;
struct Record {
    uint8_t version;
    float pulsesPerMm, pitch, inset;
    BoardGeometry geometry;
    uint16_t crc;
};
uint16_t checksum(const Record &record) {
    const uint8_t *p = reinterpret_cast<const uint8_t *>(&record);
    uint16_t crc = 0xFFFF;
    for (uint8_t i = 0; i < offsetof(Record, crc); ++i) {
        crc ^= static_cast<uint16_t>(p[i]) << 8;
        for (uint8_t b = 0; b < 8; ++b)
            crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
    }
    return crc;
}
}
bool loadBoardGeometry(BoardGeometry &geometry) {
    if (EEPROM.read(0) != COMMITTED) return false;
    Record record;
    EEPROM.get(1, record);
    if (record.version != 1 || record.crc != checksum(record) ||
        record.pulsesPerMm != Config::PULSES_PER_MM ||
        record.pitch != Config::BOARD_SQUARE_MM || record.inset != Config::END_CLEARANCE_MM)
        return false;
    for (uint8_t i = 0; i < 2; ++i) {
        const float span = record.geometry.span[i], origin = record.geometry.origin[i];
        if (!isfinite(span) || !isfinite(origin) || span < Config::CALIBRATION_MIN_TRAVEL_MM ||
            span > Config::CALIBRATION_MAX_TRAVEL_MM ||
            origin + 0.5f * Config::BOARD_SQUARE_MM < Config::END_CLEARANCE_MM ||
            origin + 7.5f * Config::BOARD_SQUARE_MM > span - Config::END_CLEARANCE_MM)
            return false;
    }
    geometry = record.geometry;
    return true;
}
void saveBoardGeometry(const BoardGeometry &geometry) {
    Record record = {};
    record.version = 1;
    record.pulsesPerMm = Config::PULSES_PER_MM;
    record.pitch = Config::BOARD_SQUARE_MM;
    record.inset = Config::END_CLEARANCE_MM;
    record.geometry = geometry;
    record.crc = checksum(record);
    // A power loss during replacement leaves an invalid record, never partial geometry.
    EEPROM.update(0, 0);
    EEPROM.put(1, record);
    EEPROM.update(0, COMMITTED);
}
