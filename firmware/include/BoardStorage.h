#pragma once
#include "TableGeometry.h"

// Geometry survives power loss; a saved coordinate system is not a live home.
// No carriage position or piece occupancy is restored from EEPROM.
struct BoardGeometry {
    float span[2];
    float origin[2];
};
bool loadBoardGeometry(BoardGeometry &geometry);
void saveBoardGeometry(const BoardGeometry &geometry);
