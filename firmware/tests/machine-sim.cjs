const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const { CPU, avrInstruction, AVRTimer, timer0Config, timer1Config, AVRIOPort,
  portBConfig, portCConfig, portDConfig, AVRUSART, usart0Config,
  AVREEPROM, EEPROMMemoryBackend } = require('avr8js');
const smallTableTest = process.argv.includes('--small-table');
const narrowReleaseTest = process.argv.includes('--narrow-release');
const restoreTest = process.argv.includes('--restore');
const badStorage = process.argv.includes('--corrupt') || process.argv.includes('--interrupted-save') ||
  process.argv.includes('--config-change');
const hexPath = process.argv.slice(2).find(arg => !arg.startsWith('--')) ||
  path.join(__dirname, '../.pio/build/uno/firmware.hex');
const flash = new Uint16Array(16384);
const bytes = new Uint8Array(flash.buffer);
let base = 0;
for (const line of fs.readFileSync(hexPath, 'utf8').trim().split(/\r?\n/)) {
  const count = parseInt(line.slice(1, 3), 16), address = parseInt(line.slice(3, 7), 16);
  const type = parseInt(line.slice(7, 9), 16);
  if (type === 4) base = parseInt(line.slice(9, 13), 16) * 65536;
  if (type === 0) for (let i = 0; i < count; i++)
    bytes[base + address + i] = parseInt(line.slice(9 + i * 2, 11 + i * 2), 16);
}
const cpu = new CPU(flash);
const eeprom = new EEPROMMemoryBackend(1024);
const eepromFile = path.join(__dirname, '../.pio/storage-test.eeprom');
if (restoreTest || badStorage) {
  eeprom.memory.set(fs.readFileSync(eepromFile));
  if (process.argv.includes('--corrupt')) eeprom.memory[20] ^= 1;
  if (process.argv.includes('--interrupted-save')) eeprom.memory[0] = 0;
  if (process.argv.includes('--config-change')) {
    const record = Buffer.from(eeprom.memory.buffer);
    record.writeFloatLE(100, 2); // A different pulses/mm, with a valid checksum.
    let crc = 0xffff;
    for (let i=1;i<30;i++) {
      crc ^= record[i] << 8;
      for (let bit=0;bit<8;bit++) crc=((crc<<1)^((crc&0x8000)?0x1021:0))&0xffff;
    }
    record.writeUInt16LE(crc,30);
  }
}
new AVREEPROM(cpu, eeprom);
new AVRTimer(cpu, timer0Config);
new AVRTimer(cpu, timer1Config);
const portB = new AVRIOPort(cpu, portBConfig);
new AVRIOPort(cpu, portCConfig);
const portD = new AVRIOPort(cpu, portDConfig);
const uart = new AVRUSART(cpu, usart0Config, 16000000);
let serial = '';
let relayHigh = false;
let modelEnabled = false;
let modelDirty = false;
let modelFault = '';
let injectedLimit = null;
const physical = [16667, restoreTest || narrowReleaseTest ? 16667 : 33595]; // Restore starts away from either end.
const stroke = smallTableTest ? [26667, 33595] :
  narrowReleaseTest ? [33333, 33333] : [33478, 33595];
// Default model matches the measured usable 247.085 x 247.963 mm, before
// the pulse model's sub-pulse/stop-edge uncertainty.
let trackBoard = false;
let trackPiece = false;
let maxCrossTrack = 0;
let expectedRoute = [];
let boardOrigins = [0, 0];
let piecePulses = [[], []];
let relayEvents = [];
uart.onByteTransmit = b => serial += String.fromCharCode(b);
const axes = [0, 1].map(() => ({ pulses: [], forward: false, dirAt: 0,
  highAt: null, lowAt: 0, minHigh: Infinity, minLow: Infinity, minDir: Infinity,
  stopPulseDeadline: 0, truncatedStopPulses: 0 }));
portD.addListener((value, old) => {
  relayHigh = !!(value & 4); // Active-HIGH relay on D2.
  if ((value ^ old) & 4) relayEvents.push({on: relayHigh, at: cpu.cycles, physical: [...physical]});
  for (let i = 0; i < 2; i++) {
    const mask = 1 << (i === 0 ? 3 : 5);
    if ((value ^ old) & mask) axes[i].dirAt = cpu.cycles;
    axes[i].forward = !!(value & mask);
  }
});
portB.addListener((value, old) => {
  for (let i = 0; i < 2; i++) {
    const a = axes[i], mask = 1 << (i + 1); // D9=PB1, D10=PB2.
    if (!(old & mask) && (value & mask)) {
      const low = (cpu.cycles - a.lowAt) / 16, dir = (cpu.cycles - a.dirAt) / 16;
      assert(low >= 1.5, `axis ${i}: minimum STEP low width`);
      assert(dir >= 5, `axis ${i}: minimum DIR setup time`);
      a.minLow = Math.min(a.minLow, low);
      a.minDir = Math.min(a.minDir, dir);
      a.highAt = cpu.cycles;
      a.pulses.push({ forward: a.forward, at: cpu.cycles });
      if (modelEnabled) {
        physical[i] += a.forward ? 1 : -1;
        modelDirty = true;
        if (trackBoard) {
          assert(physical[i] * 0.0075 >= 1.98 && physical[i] * 0.0075 <= stroke[i] * 0.0075 - 1.98,
            'board pulse stays within measured inset');
          if (!trackPiece) assert(!relayHigh, 'travel/dry route keeps relay off');
        }
        if (trackPiece && relayHigh) {
          piecePulses[i].push({forward: a.forward, at: cpu.cycles});
          const xy = physical.map(pos => pos * 0.0075);
          let distance = Infinity;
          for (let j=0; j<expectedRoute.length-1; j++) distance = Math.min(distance, segmentDistance(xy, expectedRoute[j], expectedRoute[j+1]));
          maxCrossTrack = Math.max(maxCrossTrack, distance);
          assert(distance < 0.30, 'loaded path remains within 0.30 mm of planned segments: ' + distance);
        }
      }
    }
    if ((old & mask) && !(value & mask)) {
      if (a.highAt !== null) {
        const high = (cpu.cycles - a.highAt) / 16;
        // forceStopAndNewPosition deliberately disconnects the timer output
        // immediately. It can truncate the final HIGH if x lands on that pulse;
        // enforce timing on normal pulses and permit only ONE such final pulse
        // per axis within 5 ms of x/o or a newly triggered limit. Stop tests
        // separately assert that no queued pulses continue afterward.
        if (high < 1.5 && cpu.cycles <= a.stopPulseDeadline) {
          a.stopPulseDeadline = 0;
          a.truncatedStopPulses++;
        } else {
          assert(high >= 1.5, `axis ${i}: minimum STEP high width (${high} us)`);
          a.minHigh = Math.min(a.minHigh, high);
        }
      }
      a.highAt = null;
      a.lowAt = cpu.cycles;
    }
  }
});
function run(ms) {
  const end = cpu.cycles + ms * 16000;
  while (cpu.cycles < end) {
    avrInstruction(cpu); cpu.tick();
    if (modelEnabled && modelDirty) { modelDirty = false; updateModel(); }
  }
}
function until(condition, timeoutMs = 2000) {
  const end = cpu.cycles + timeoutMs * 16000;
  while (!condition() && cpu.cycles < end) run(5);
  assert(condition(), 'Timed out: ' + serial.slice(-600));
}
function send(text) {
  for (const ch of text) {
    if ('xXoO'.includes(ch)) for (const a of axes) a.stopPulseDeadline = cpu.cycles + 5 * 16000;
    assert(uart.writeByte(ch.charCodeAt(0))); run(1);
  }
}
const count = i => axes[i].pulses.length;
const counts = () => [count(0), count(1)];
function command(text, wait = 30) { serial = ''; send(text); run(wait); }
const limitStates = [[false,false],[false,false]];
function setLimit(i, end, triggered) {
  const slot=end==='min'?0:1;
  if(triggered && !limitStates[i][slot])
    for(const a of axes) a.stopPulseDeadline=cpu.cycles+5*16000;
  limitStates[i][slot]=triggered;
  const high = !triggered; // NO switch: released=HIGH, pressed=LOW.
  if (i === 1 && end === 'max') portB.setPin(0, high); // D8
  else portD.setPin(i === 0 ? (end === 'min' ? 4 : 7) : 6, high);
}
function allLimitsReleased() {
  for (let i = 0; i < 2; i++) for (const end of ['min', 'max']) setLimit(i, end, false);
}
function updateModel() {
  for (let i = 0; i < 2; i++) {
    setLimit(i, 'min', (injectedLimit?.i === i && injectedLimit.end === 'min') ||
      modelFault === 'stuck-min' && i === 0 || physical[i] <= 0);
    setLimit(i, 'max', (injectedLimit?.i === i && injectedLimit.end === 'max') ||
      (modelFault === 'missing-max' && i === 0 ? false : physical[i] >= stroke[i]));
  }
}

function segmentDistance(p,a,b) {
  const dx=b[0]-a[0], dy=b[1]-a[1], length=dx*dx+dy*dy;
  const t=Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length));
  return Math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy);
}

const origin = [(stroke[0] * .0075 - 250) / 2, (stroke[1] * .0075 - 250) / 2];
const value17 = c => c <= '9' ? c.charCodeAt(0) - 48 : c.charCodeAt(0) - 55;
function routeMm(encoded) {
  const points = [];
  for (let i = 0; i < encoded.length; i += 2) {
    const xy = [value17(encoded[i]), value17(encoded[i + 1])];
    points.push(xy.map((v, axis) => {
      const mm = origin[axis] + v * 31.25 / 2;
      return v === 0 ? Math.max(mm, 2) : v === 16 ? Math.min(mm, stroke[axis] * .0075 - 2) : mm;
    }));
  }
  return points;
}
function withDeliveryOvershoot(points) {
  const previous = points.at(-2), nominal = points.at(-1);
  const dx = nominal[0] - previous[0], dy = nominal[1] - previous[1];
  const length = Math.hypot(dx, dy);
  assert(length > 0, 'final loaded leg has a direction');
  for (const overshoot of [6.25, 5.625]) {
    const release = [nominal[0] + overshoot * dx / length,
      nominal[1] + overshoot * dy / length];
    if (release.every((mm, axis) => {
      const pulses = Math.round(mm / .0075);
      return pulses >= Math.ceil(2 / .0075) &&
        pulses <= Math.floor((stroke[axis] * .0075 - 2) / .0075);
    })) return [...points.slice(0, -1), release];
  }
  throw new Error('no compensated release target inside measured bounds');
}
function mapHex(locations) {
  const bytes = Buffer.alloc(12);
  for (const id of locations) bytes[id >> 3] |= 1 << (id & 7);
  return bytes.toString('hex').toUpperCase();
}
let nextId = 1;
function request(verb, wait = 35) {
  const id = nextId++;
  command('@' + id + ' ' + verb + '\n', wait);
  assert(serial.includes('@' + id + ' ACK'), serial.slice(-600));
  return id;
}
function expectDone(id, verb, timeout = 2000) {
  // Match the complete terminal line, as the host does. Matching the verb
  // before its newline can race the controller's pending-state cleanup.
  until(() => serial.includes('@' + id + ' DONE ' + verb + '\r\n'), timeout);
}
function expectError(id, code) {
  assert(serial.includes('@' + id + ' ERR ' + code), serial.slice(-600));
}
function noMotion() {
  const old = counts(); run(500);
  assert.deepEqual(counts(), old, 'rejected request does not move');
  assert(!relayHigh, 'rejected request leaves magnet off');
}
function map(locations) {
  const id = request('MAP ' + mapHex(locations));
  expectDone(id, 'MAP');
}
function carry(source, destination, encoded, timeout = 25000) {
  const nominalRoute = routeMm(encoded);
  expectedRoute = destination < 64 ? withDeliveryOvershoot(nominalRoute) : nominalRoute;
  maxCrossTrack = 0;
  piecePulses = [[], []];
  trackPiece = true; relayEvents = [];
  const id = request('CARRY ' + source + ' ' + destination + ' ' + encoded);
  expectDone(id, 'CARRY', timeout); run(80);
  assert.equal(relayEvents.length, 2, 'one pickup and one release');
  assert(relayEvents[0].on && !relayEvents[1].on);
  for (let axis = 0; axis < 2; axis++) {
    assert(Math.abs(relayEvents[0].physical[axis] * .0075 - expectedRoute[0][axis]) < .05);
    assert(Math.abs(relayEvents[1].physical[axis] * .0075 - expectedRoute.at(-1)[axis]) < .05);
  }
  trackPiece = false;
  return id;
}
function offsetPointMm(square, corner) {
  const x = 2 * (square % 8) + 1 + (corner[1] === 'E' ? .6 : -.6);
  const y = 2 * Math.floor(square / 8) + 1 + (corner[0] === 'N' ? .6 : -.6);
  return [origin[0] + x * 31.25 / 2, origin[1] + y * 31.25 / 2];
}
function offset(square, corner, direction, timeout = 10000) {
  const centerCode = [2 * (square % 8) + 1, 2 * Math.floor(square / 8) + 1]
    .map(v => v.toString(17).toUpperCase()).join('');
  const center = routeMm(centerCode)[0];
  const edge = offsetPointMm(square, corner);
  expectedRoute = withDeliveryOvershoot(direction === 'OUT' ? [center, edge] : [edge, center]);
  maxCrossTrack = 0; piecePulses = [[], []];
  trackPiece = true; relayEvents = [];
  const id = request('OFFSET ' + square + ' ' + corner + ' ' + direction);
  expectDone(id, 'OFFSET', timeout); run(80);
  assert.equal(relayEvents.length, 2, 'offset picks up and releases once');
  assert(relayEvents[0].on && !relayEvents[1].on);
  for (let axis = 0; axis < 2; axis++) {
    assert(Math.abs(relayEvents[0].physical[axis] * .0075 - expectedRoute[0][axis]) < .05);
    assert(Math.abs(relayEvents[1].physical[axis] * .0075 - expectedRoute[1][axis]) < .05);
  }
  trackPiece = false;
  return id;
}
function carryOffset(source, corner, destination, encoded, timeout = 25000) {
  expectedRoute = withDeliveryOvershoot([offsetPointMm(source, corner), ...routeMm(encoded).slice(1)]);
  maxCrossTrack = 0; piecePulses = [[], []];
  trackPiece = true; relayEvents = [];
  const id = request('CARRY_OFFSET ' + source + ' ' + corner + ' ' + destination + ' ' + encoded);
  expectDone(id, 'CARRY_OFFSET', timeout); run(80);
  assert.equal(relayEvents.length, 2, 'offset-source carry picks up and releases once');
  assert(relayEvents[0].on && !relayEvents[1].on);
  for (let axis = 0; axis < 2; axis++) {
    assert(Math.abs(relayEvents[0].physical[axis] * .0075 - expectedRoute[0][axis]) < .05);
    assert(Math.abs(relayEvents[1].physical[axis] * .0075 - expectedRoute.at(-1)[axis]) < .05);
  }
  trackPiece = false;
  return id;
}
function loadedAxisProfile(axis) {
  // Measure the actual STEP pulse timing from the compiled firmware. Split
  // waypoint legs at their dwell gaps, then inspect the longest loaded leg.
  const runs = [];
  let current = [];
  for (const pulse of piecePulses[axis]) {
    if (current.length && (pulse.at - current.at(-1).at) / 16000 > 50) {
      runs.push(current); current = [];
    }
    current.push(pulse);
  }
  if (current.length) runs.push(current);
  const longest = runs.reduce((a, b) => b.length > a.length ? b : a, []);
  assert(longest.length > 5000, 'long loaded segment recorded');
  function speedAt(index) {
    const halfWindow = 20;
    const pulseSpan = 2 * halfWindow;
    const seconds = (longest[index + halfWindow].at -
      longest[index - halfWindow].at) / 16000000;
    return pulseSpan * .0075 / seconds;
  }
  return {early: speedAt(133), cruise: speedAt(4000)};
}
function checkLoadedProfile() {
  // The d5-to-P00 fixture contains a 93.75 mm open horizontal leg.
  const {early, cruise} = loadedAxisProfile(0);
  assert(early >= 23 && early <= 34, `loaded ramp near 1 mm: ${early} mm/s`);
  assert(cruise >= 47 && cruise <= 53, `loaded cruise: ${cruise} mm/s`);
  assert(cruise > early * 1.4, 'loaded carry accelerates gradually');
}
function checkGeom() {
  const id = request('GEOM');
  const match = serial.match(new RegExp('@' + id +
    ' DONE GEOM X=([-0-9.]+) Y=([-0-9.]+) OX=([-0-9.]+) OY=([-0-9.]+) P=([-0-9.]+) I=([-0-9.]+) PPM=([-0-9.]+)'));
  assert(match, serial.slice(-600));
  const values = match.slice(1).map(Number);
  assert(Math.abs(values[0] - stroke[0] * .0075) < .05);
  assert(Math.abs(values[1] - stroke[1] * .0075) < .05);
  assert(Math.abs(values[2] - (values[0] - 250) / 2) < .02);
  assert(Math.abs(values[3] - (values[1] - 250) / 2) < .02);
  assert(Math.abs(values[4] - 31.25) < .0001);
  assert(Math.abs(values[5] - 2) < .0001);
  assert(Math.abs(values[6] - 133.3333) < .0001);
}

allLimitsReleased(); run(200);
assert(serial.includes('!BOOT V=1'));
assert.deepEqual(counts(), [0, 0]);
assert(!relayHigh);
modelEnabled = true; updateModel();

let id = request('HELLO');
assert(serial.includes('@' + id + ' DONE HELLO V=1 CAPS=STATUS,HOME,REF,STOP,MAP,CARRY,CARRY_DIAG,CARRY_DIRECT,CARRY_OFFSET,OFFSET,SAVE,GEOM,OVER20'));
id = request('STATUS');
assert(serial.includes('DONE STATUS REF=0 BUSY=0 MAP=0 SAVED=' + (restoreTest ? 1 : 0)));
id = request('GEOM'); expectError(id, 'REFERENCE');
command('@32767 STATUS\n'); assert(serial.includes('@32767 DONE STATUS'));
command('@32768 STATUS\n'); assert(serial.includes('@32768 DONE STATUS'));
command('@32767 STATUS\n'); assert(serial.includes('@32767 ERR DUPLICATE'));
nextId = 32769;
console.log('PASS boot, handshake, unknown reference/map state');

if (badStorage) {
  id = request('REF'); expectError(id, 'NO_GEOMETRY'); noMotion();
  console.log('PASS damaged/incomplete/incompatible EEPROM cannot authorize REF');
} else if (restoreTest) {
  id = request('CARRY 35 64 79680800'); expectError(id, 'REFERENCE'); noMotion();
  id = request('REF');
  assert(!serial.includes('@' + id + ' DONE REF'), 'REF completion must be asynchronous');
  expectDone(id, 'REF', 55000);
  for (let axis = 0; axis < 2; axis++)
    assert(Math.abs(physical[axis] * .0075 - 2) < .03, 'REF parks at MIN inset');
  id = request('STATUS');
  assert(serial.includes('DONE STATUS REF=1 BUSY=0 MAP=0 SAVED=1'));
  checkGeom();
  console.log('PASS fresh reboot restores saved geometry but requires REF and MAP');
} else if (smallTableTest) {
  id = request('HOME');
  until(() => serial.includes('@' + id + ' ERR GEOMETRY'), 100000);
  id = request('STATUS');
  assert(serial.includes('DONE STATUS REF=0'));
  console.log('PASS undersized measured table cannot authorize board travel');
} else {
  id = request('CARRY 1 18 31424455'); expectError(id, 'REFERENCE'); noMotion();
  id = request('SAVE'); expectError(id, 'REFERENCE');
  id = request('MAP FFFF00000000FFFF00000000'); expectDone(id, 'MAP');
  id = request('MAP ffff00000000ffff00000000'); expectError(id, 'SYNTAX');
  id = request('MAP 1234'); expectError(id, 'SYNTAX');
  id = request('CARRY 1 18 31Z24455'); expectError(id, 'SYNTAX');
  id = request('CARRY 1 18 314244550'); expectError(id, 'SYNTAX');
  id = request('CARRY 256 18 31424455'); expectError(id, 'SYNTAX');
  id = request('CARRY 1 18 ' + '31'.repeat(13)); expectError(id, 'SYNTAX');
  id = request('OFFSET 7 NE OUT'); expectError(id, 'REFERENCE'); noMotion();
  id = request('OFFSET 64 NE OUT'); expectError(id, 'SYNTAX');
  id = request('OFFSET 7 XW OUT'); expectError(id, 'SYNTAX');
  id = request('OFFSET 7 NE SIDE'); expectError(id, 'SYNTAX');
  id = request('CARRY_OFFSET 7 NE 5 F1G2E4C2B1'); expectError(id, 'REFERENCE'); noMotion();
  id = request('CARRY_OFFSET 64 NE 5 F1G2E4C2B1'); expectError(id, 'SYNTAX');
  id = request('CARRY_OFFSET 7 NE 64 F1G2E4C2B1'); expectError(id, 'SYNTAX');
  id = request('CARRY_OFFSET 7 NX 5 F1G2E4C2B1'); expectError(id, 'SYNTAX');
  id = request('HOME');
  assert(!serial.includes('@' + id + ' DONE HOME'), 'HOME completion must be asynchronous');
  const homeId = id;
  id = request('MAP ' + mapHex([1])); expectError(id, 'BUSY');
  id = request('CARRY 1 18 31424455'); expectError(id, 'BUSY');
  id = request('HELLO'); expectError(id, 'BUSY');
  id = request('GEOM'); expectError(id, 'BUSY');
  expectDone(homeId, 'HOME', 100000); run(80);
  for (let axis = 0; axis < 2; axis++)
    assert(Math.abs(physical[axis] * .0075 - 2) < .03, 'HOME parks at MIN inset');
  checkGeom();
  if (narrowReleaseTest) {
    map([7]);
    offset(7, 'NE', 'OUT');
    const nominal = offsetPointMm(7, 'NE');
    const actual = relayEvents[1].physical.map(pulses => pulses * .0075);
    assert(Math.abs(Math.hypot(actual[0] - nominal[0], actual[1] - nominal[1]) - 5.625) < .05,
      'outer-square release retries at 90% inside measured travel');
    console.log('PASS outer-square OFFSET OUT uses bounded 90% release retry');
    process.exit(0);
  }
  id = request('SAVE'); expectDone(id, 'SAVE');
  assert.equal(eeprom.memory[0], 0xa7);
  fs.writeFileSync(eepromFile, eeprom.memory);
  id = request('STATUS');
  assert(serial.includes('DONE STATUS REF=1 BUSY=0 MAP=1 SAVED=1'));
  console.log('PASS MAP syntax/busy, asynchronous HOME, and saved geometry');

  trackBoard = true;
  const unmoved = counts();
  id = request('CARRY 1 18 3142445G'); expectError(id, 'ROUTE'); noMotion();
  id = request('CARRY 1 18 314455'); expectError(id, 'ROUTE'); noMotion();
  id = request('CARRY 1 18 313355'); expectError(id, 'ROUTE'); noMotion();
  assert.deepEqual(counts(), unmoved, 'invalid routes rejected before travel');
  carry(1, 18, '31424455');
  id = request('CARRY 1 18 31424455'); expectError(id, 'MAP'); noMotion();
  console.log('PASS square-to-square carry commits occupancy only after release');

  // Whole center-to-center routes may move one or two squares on a rank,
  // file, or 45-degree diagonal, including their normal delivery overshoot.
  for (const [destination, encoded] of [
    [19, '5575'], [20, '5595'], [26, '5557'], [34, '5559'],
    [27, '5577'], [36, '5599'],
  ]) {
    map([18]);
    carry(18, destination, encoded);
    id = request('CARRY 18 ' + destination + ' ' + encoded);
    expectError(id, 'MAP'); noMotion();
  }
  map([36]);
  carry(36, 18, '9955');
  function rejectDirect(verb) {
    const before = counts(), relayCount = relayEvents.length;
    const rejected = request(verb);
    expectError(rejected, 'ROUTE'); noMotion();
    assert.deepEqual(counts(), before, 'invalid direct route rejected before any travel');
    assert.equal(relayEvents.length, relayCount, 'invalid direct route never energizes magnet');
  }
  map([18, 19]);
  rejectDirect('CARRY 18 20 5595'); // d3 blocks the two-square rank.
  map([18, 26]);
  rejectDirect('CARRY 18 34 5559'); // c4 blocks the two-square file.
  map([18, 27]);
  rejectDirect('CARRY 18 36 5599'); // d4 blocks the two-square diagonal.
  map([18]);
  rejectDirect('CARRY 18 21 55B5'); // Three squares.
  rejectDirect('CARRY 18 28 5597'); // Knight-shaped center leg.
  rejectDirect('CARRY 18 20 557595'); // A direct leg embedded in a longer route.
  rejectDirect('CARRY_OFFSET 18 NE 19 5575'); // Actual offset direction differs.
  map([0, 65]);
  rejectDirect('CARRY 0 1 1131'); // P01 lies within 18 mm of the loaded rank.
  map([1, 64]);
  rejectDirect('CARRY 1 0 3111'); // P00 clears the nominal leg but blocks overshoot.
  console.log('PASS bounded direct rank/file/diagonal carry, occupancy, clearance, offset, and no-motion guards');

  // The nominal final leg clears P01, but extending from a1 toward (2,0)
  // would pass within one piece diameter of that occupied parking center.
  map([9, 65]);
  id = request('CARRY 9 0 33220211'); expectError(id, 'ROUTE'); noMotion();
  console.log('PASS occupied parking slot blocks compensated release before motion');

  // A rook nudges inside h1 while h2 and even the h1 corner parking slot P08
  // are occupied. The adjacent P25 slot would be too close to the NE target.
  map([7, 15, 72, 89]);
  id = request('OFFSET 7 NE OUT'); expectError(id, 'ROUTE'); noMotion();
  map([7, 15, 72]);
  id = request('OFFSET 0 NW OUT'); expectError(id, 'MAP'); noMotion();
  offset(7, 'NE', 'OUT');
  id = request('STATUS'); assert(serial.includes('@' + id + ' DONE STATUS REF=1 BUSY=0 MAP=1'));
  offset(7, 'NE', 'IN');
  offset(7, 'NE', 'OUT');
  offset(7, 'NE', 'IN');
  console.log('PASS in-square OFFSET, occupied corner parking, clearance guard, and unchanged MAP');

  // The rook starts at h1's NE corner, with its corner parking slot occupied.
  // A single command picks it up there and carries to f1 after the king has
  // moved to g1. The encoded center is only an anchor for route validation.
  map([7, 6, 72]);
  offset(7, 'NE', 'OUT');
  id = request('CARRY_OFFSET 7 NE 5 F1G2E4C2B3'); expectError(id, 'ROUTE'); noMotion();
  id = request('CARRY_OFFSET 7 NE 6 F1G2E4C2D1'); expectError(id, 'MAP'); noMotion();
  carryOffset(7, 'NE', 5, 'F1G2E4C2B1');
  id = request('CARRY 7 5 F1G2E4C2B1'); expectError(id, 'MAP'); noMotion();
  console.log('PASS CARRY_OFFSET direct corner pickup, occupied parking, route/map validation, and committed occupancy');

  // Direct grid diagonal is legal only when it is 45 degrees and crosses no
  // occupied center. The d4 center lies halfway between grid points 66 and 88.
  map([18, 27]);
  id = request('CARRY 18 36 55668899'); expectError(id, 'ROUTE'); noMotion();
  map([18]);
  id = request('CARRY 18 36 55668A99'); expectError(id, 'ROUTE'); noMotion();
  carry(18, 36, '55668899');
  console.log('PASS 45-degree grid diagonal and occupied-center guard');

  // A victim at c3 must leave before the knight at b1 can occupy c3.
  map([1, 18]);
  id = request('CARRY 1 18 31424455'); expectError(id, 'MAP'); noMotion();
  carry(18, 64, '55440400');
  carry(1, 18, '31424455');
  console.log('PASS capture order: occupied target rejected, victim parked first');

  // Cross-language host vector: d5 (35) to P00 (64), encoded 79->68->08->00.
  map([35]);
  const block = mapHex([35, 85]); // P21 at (0,8) blocks the long grid edge.
  id = request('MAP ' + block); expectDone(id, 'MAP');
  id = request('CARRY 35 64 79680800'); expectError(id, 'ROUTE'); noMotion();
  map([35]);
  carry(35, 64, '79680800');
  checkLoadedProfile();
  carry(64, 35, '00086879');
  console.log('PASS square-to-parking vector, gentler loaded profile, perimeter blocker, and retrieval');

  // c5 and c4 stand on opposite sides of the horizontal grid lane y=8,
  // x=6..0. That lane gets a 30 mm/s cap; otherwise the same route above uses
  // 50 mm/s. One-sided adjacency also slows conservatively. A neighboring c4
  // does not slow a vertical lane, and a diagonal remains at normal speed.
  map([35, 34, 26]);
  carry(35, 64, '79680800');
  let profile = loadedAxisProfile(0);
  assert(profile.early >= 17 && profile.early <= 27, `two-sided horizontal ramp: ${profile.early} mm/s`);
  assert(profile.cruise >= 27 && profile.cruise <= 33, `two-sided horizontal cruise: ${profile.cruise} mm/s`);
  map([35, 34]);
  carry(35, 64, '79680800');
  profile = loadedAxisProfile(0);
  assert(profile.early >= 17 && profile.early <= 27, `tight horizontal ramp: ${profile.early} mm/s`);
  assert(profile.cruise >= 27 && profile.cruise <= 33, `tight horizontal cruise: ${profile.cruise} mm/s`);
  map([35, 26]);
  carry(35, 18, '79686455');
  profile = loadedAxisProfile(1);
  assert(profile.cruise >= 47 && profile.cruise <= 53, `vertical cruise: ${profile.cruise} mm/s`);
  map([35, 34]);
  carry(35, 16, '79682415');
  profile = loadedAxisProfile(0);
  assert(profile.cruise >= 32 && profile.cruise <= 38, `diagonal axis cruise: ${profile.cruise} mm/s`);
  console.log('PASS occupied horizontal lane slows to 30 mm/s; open, vertical, diagonal remain at 50 mm/s');

  // A 1 ms pulse during actual travel must not cancel; 10 ms during release
  // dwell is ignored even though it exceeds the movement threshold.
  map([35]);
  expectedRoute = routeMm('79680800'); maxCrossTrack = 0;
  trackPiece = true; relayEvents = [];
  const pulseId = request('CARRY 35 64 79680800');
  command('@' + pulseId + ' CARRY 35 64 79680800\n');
  assert(serial.includes('@' + pulseId + ' ACK') && !serial.includes('ERR DUPLICATE'));
  until(() => relayHigh, 9000);
  const pickupCounts = counts(); run(400);
  until(() => counts()[0] > pickupCounts[0] || counts()[1] > pickupCounts[1], 3000);
  injectedLimit = {i: 1, end: 'max'}; updateModel(); run(1);
  injectedLimit = null; updateModel();
  until(() => relayEvents.length === 2, 25000);
  injectedLimit = {i: 1, end: 'max'}; updateModel(); run(10);
  injectedLimit = null; updateModel();
  expectDone(pulseId, 'CARRY', 1000);
  trackPiece = false;
  assert.equal(relayEvents.length, 2);
  console.log('PASS short moving pulse and sustained release-dwell contact ignored');

  id = request('STATUS'); assert(serial.includes('REF=1 BUSY=0 MAP=1'));
  map([35]);
  expectedRoute = routeMm('79680800'); trackPiece = true; relayEvents = [];
  const faultId = request('CARRY 35 64 79680800');
  until(() => relayHigh, 9000);
  const faultCounts = counts(); run(400);
  until(() => counts()[0] > faultCounts[0] || counts()[1] > faultCounts[1], 3000);
  injectedLimit = {i: 1, end: 'max'}; updateModel(); run(5);
  until(() => serial.includes('@' + faultId + ' ERR MOTION'), 1000);
  injectedLimit = null; updateModel();
  const stopped = counts(); run(700);
  assert.deepEqual(counts(), stopped); assert(!relayHigh);
  trackPiece = false;
  id = request('STATUS'); assert(serial.includes('REF=0 BUSY=0 MAP=0'));
  console.log('PASS sustained moving contact stops both axes and invalidates map/reference');

  // Saved MIN-only reference allows a second carry without measuring MAX.
  trackBoard = false;
  id = request('REF'); expectDone(id, 'REF', 55000);
  trackBoard = true;
  map([7]);
  const offsetStopId = request('OFFSET 7 NE OUT');
  until(() => relayHigh, 9000);
  id = request('STOP'); expectDone(id, 'STOP');
  assert(serial.includes('@' + offsetStopId + ' ERR CANCELED'));
  let stopCounts = counts(); run(700);
  assert.deepEqual(counts(), stopCounts); assert(!relayHigh);
  id = request('STATUS'); assert(serial.includes('REF=0 BUSY=0 MAP=0'));
  console.log('PASS STOP cancels OFFSET and invalidates map/reference');

  trackBoard = false;
  id = request('REF'); expectDone(id, 'REF', 55000);
  trackBoard = true;
  map([35]);
  const stopId = request('CARRY 35 64 79680800');
  until(() => relayHigh, 9000);
  id = request('STOP');
  expectDone(id, 'STOP');
  assert(serial.includes('@' + stopId + ' ERR CANCELED'));
  stopCounts = counts(); run(700);
  assert.deepEqual(counts(), stopCounts); assert(!relayHigh);
  id = request('STATUS'); assert(serial.includes('REF=0 BUSY=0 MAP=0'));
  console.log('PASS STOP cancels CARRY with one terminal error');

  trackBoard = false;
  id = request('REF'); expectDone(id, 'REF', 55000);
  trackBoard = true;
  map([7, 6, 72]);
  offset(7, 'NE', 'OUT');
  expectedRoute = [offsetPointMm(7, 'NE'), ...routeMm('F1G2E4C2B1').slice(1)];
  trackPiece = true; relayEvents = [];
  const offsetCarryStopId = request('CARRY_OFFSET 7 NE 5 F1G2E4C2B1');
  until(() => relayHigh, 9000);
  id = request('STOP'); expectDone(id, 'STOP');
  assert(serial.includes('@' + offsetCarryStopId + ' ERR CANCELED'));
  stopCounts = counts(); run(700);
  assert.deepEqual(counts(), stopCounts); assert(!relayHigh);
  trackPiece = false;
  id = request('STATUS'); assert(serial.includes('REF=0 BUSY=0 MAP=0'));
  console.log('PASS STOP cancels CARRY_OFFSET and invalidates map/reference');

  // Homing uses the first raw contact, even a pulse shorter than board debounce.
  trackBoard = false;
  const rawBefore = counts();
  id = request('HOME');
  until(() => counts()[0] > rawBefore[0], 3000);
  injectedLimit = {i: 0, end: 'min'}; updateModel(); run(1);
  injectedLimit = null; updateModel();
  until(() => serial.includes('@' + id + ' ERR CAL_X'), 3000);
  console.log('PASS homing raw edge is still immediate');
}
console.log('PASS compiled Uno machine protocol and limit behavior');
