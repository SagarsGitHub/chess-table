const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const { CPU, avrInstruction, AVRTimer, timer0Config, timer1Config, AVRIOPort,
  portBConfig, portCConfig, portDConfig, AVRUSART, usart0Config,
  AVREEPROM, EEPROMMemoryBackend } = require('avr8js');
const smallTableTest = process.argv.includes('--small-table');
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
const physical = [16667, restoreTest ? 16667 : 33595]; // Restore starts away from either end.
const stroke = smallTableTest ? [26667, 33595] : [33478, 33595];
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
  until(() => serial.includes('@' + id + ' DONE ' + verb), timeout);
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
  expectedRoute = routeMm(encoded); maxCrossTrack = 0;
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
assert(serial.includes('@' + id + ' DONE HELLO V=1 CAPS=STATUS,HOME,REF,STOP,MAP,CARRY,SAVE,GEOM'));
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
  carry(64, 35, '00086879');
  console.log('PASS square-to-parking vector, perimeter blocker, and retrieval');

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
  map([35]);
  const stopId = request('CARRY 35 64 79680800');
  until(() => relayHigh, 9000);
  id = request('STOP');
  expectDone(id, 'STOP');
  assert(serial.includes('@' + stopId + ' ERR CANCELED'));
  const stopCounts = counts(); run(700);
  assert.deepEqual(counts(), stopCounts); assert(!relayHigh);
  id = request('STATUS'); assert(serial.includes('REF=0 BUSY=0 MAP=0'));
  console.log('PASS STOP cancels CARRY with one terminal error');

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
