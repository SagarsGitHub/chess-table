#include "KnightRoute.h"
#include "PieceMap.h"
#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    unsigned valid = 0;
    for (uint8_t f = 0; f < 8; ++f) for (uint8_t r = 0; r < 8; ++r)
        for (uint8_t tf = 0; tf < 8; ++tf) for (uint8_t tr = 0; tr < 8; ++tr) {
            KnightRoute route, reverse;
            const int dx = int(tf) - f, dy = int(tr) - r;
            const bool legal = (std::abs(dx) == 1 && std::abs(dy) == 2) ||
                               (std::abs(dx) == 2 && std::abs(dy) == 1);
            assert(route.build(f, r, tf, tr) == legal);
            if (!legal) continue;
            ++valid;
            assert(reverse.build(tf, tr, f, r));
            for (int i = 0; i < 4; ++i) {
                auto p = route.points[i], q = reverse.points[3-i];
                assert(p.x == q.x && p.y == q.y);
                assert(p.x >= 1 && p.x <= 15 && p.y >= 1 && p.y <= 15);
            }
            assert(route.points[0].x == 2*f+1 && route.points[0].y == 2*r+1);
            assert(route.points[3].x == 2*tf+1 && route.points[3].y == 2*tr+1);
            for (int i : {0, 2}) {
                assert(std::abs(route.points[i+1].x-route.points[i].x) == 1);
                assert(std::abs(route.points[i+1].y-route.points[i].y) == 1);
            }
            const int laneX = route.points[2].x-route.points[1].x;
            const int laneY = route.points[2].y-route.points[1].y;
            assert((laneX == 0 && std::abs(laneY) == 2) ||
                   (laneY == 0 && std::abs(laneX) == 2));
            assert(route.points[1].x % 2 == 0 && route.points[1].y % 2 == 0);
            assert(route.points[2].x % 2 == 0 && route.points[2].y % 2 == 0);
        }
    assert(valid == 336);
    KnightRoute invalid;
    assert(!invalid.build(8,0,6,1));
    assert(!invalid.build(0,255,1,2));
    std::cout << "PASS all 4096 square pairs: 336 legal routes, all orientations, edges, reversals and boundary lanes\n";
    unsigned parkingRoutes = 0;
    bool intersections[9][9] = {};
    for (uint8_t slot = 0; slot < 32; ++slot) {
        const auto p = parkingPoint(slot);
        assert(p.x >= 0 && p.x <= 16 && p.y >= 0 && p.y <= 16);
        assert(p.x == 0 || p.x == 16 || p.y == 0 || p.y == 16);
        assert(!intersections[p.x / 2][p.y / 2]);
        intersections[p.x / 2][p.y / 2] = true;
        for (uint8_t r = 0; r < 8; ++r) for (uint8_t f = 0; f < 8; ++f) {
            KnightRoute route, reverse;
            const bool adjacent = std::abs(p.x - (2*f+1)) == 1 && std::abs(p.y - (2*r+1)) == 1;
            assert(route.buildParking(f,r,slot,false) == adjacent);
            assert(reverse.buildParking(f,r,slot,true) == adjacent);
            if (!adjacent) continue;
            ++parkingRoutes;
            assert(route.points[0].x == 2*f+1 && route.points[0].y == 2*r+1);
            assert(route.points[1].x == p.x && route.points[1].y == p.y);
            for (int i=0;i<2;++i) {
                assert(route.points[i].x == reverse.points[1-i].x);
                assert(route.points[i].y == reverse.points[1-i].y);
            }
        }
    }
    assert(parkingRoutes == 60);
    assert(!invalid.buildParking(0,0,32,false));
    PieceMap map;
    assert(!map.known());
    map.reset(true);
    for (uint8_t p=0;p<96;++p) assert(map.occupied(p) == (p<16 || (p>=48 && p<64)));
    assert(map.canMove(1,18)); assert(!map.canMove(1,11));
    map.complete(1,18); assert(map.canMove(18,1)); assert(!map.canMove(1,18));
    map.complete(0,64); assert(map.occupied(64)); assert(!map.occupied(0));
    map.invalidate(); assert(!map.canMove(64,0));
    map.reset(false);
    for (uint8_t p=0;p<96;++p) assert(!map.occupied(p));
    std::cout << "PASS 32 unique parking slots, all 60 adjacent-square routes/reversals and occupancy transitions\n";
}
