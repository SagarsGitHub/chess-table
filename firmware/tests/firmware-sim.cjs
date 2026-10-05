const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const { CPU, avrInstruction, AVRTimer, timer0Config, timer1Config, AVRIOPort,
  portBConfig, portCConfig, portDConfig, AVRUSART, usart0Config,
  AVREEPROM, EEPROMMemoryBackend } = require('avr8js');
const faultTest = process.argv.includes('--faults');
const smallTableTest = process.argv.includes('--small-table');
const fullBoardTest = process.argv.includes('--full-board');
const storageTest = process.argv.includes('--storage');
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
    setLimit(i, 'min', modelFault === 'stuck-min' && i === 0 || physical[i] <= 0);
    setLimit(i, 'max', modelFault === 'missing-max' && i === 0 ? false : physical[i] >= stroke[i]);
  }
}

function segmentDistance(p,a,b) {
  const dx=b[0]-a[0], dy=b[1]-a[1], length=dx*dx+dy*dy;
  const t=Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length));
  return Math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy);
}
function squareXY(name) {
  return [boardOrigins[0]+(name.charCodeAt(0)-97+.5)*31.25,
          boardOrigins[1]+(Number(name[1])-.5)*31.25];
}
function routeFor(from,to) {
  const a=squareXY(from), b=squareXY(to);
  const sx=Math.sign(b[0]-a[0]), sy=Math.sign(b[1]-a[1]);
  return [a,[a[0]+sx*15.625,a[1]+sy*15.625],[b[0]-sx*15.625,b[1]-sy*15.625],b];
}
function at(name) {
  const p=squareXY(name);
  for(let i=0;i<2;i++) assert(Math.abs(physical[i]*.0075-p[i])<.04, 'position at '+name);
}
function home(cmd='h') {
  trackBoard=false; trackPiece=false;
  command(cmd);
  until(()=>serial.includes('Board ready:'),100000); run(80);
  const origin=serial.match(/Board origin X,Y from MIN \(mm\): ([0-9.-]+),([0-9.-]+)/);
  assert(origin); boardOrigins=[Number(origin[1]),Number(origin[2])];
  assert(serial.includes('64 centers fit'));
  for(let i=0;i<2;i++) assert(Math.abs(physical[i]*.0075-2)<.03,'homing parks at MIN inset');
  trackBoard=true;
}
function boardMove(text, timeout=65000) {
  command(text); until(()=>serial.includes('Board motion complete.'),timeout); run(20);
}
function assertStopped() {
  const stopped=counts(); run(700);
  assert.deepEqual(counts(),stopped,'both queues remain stopped'); assert(!relayHigh);
  command('ga1\n'); assert(serial.includes('Board unavailable; run h first.'));
}
function runKnight(from,to,carry,spaced=false) {
  if(carry) command('e\ni'+from+'1\n');
  trackPiece=carry; expectedRoute=routeFor(from,to); maxCrossTrack=0;
  relayEvents=[]; piecePulses=[[],[]];
  command((carry?'k':'d')+(spaced?' '+from.toUpperCase()+' '+to.toUpperCase():from+to)+'\r\n');
  const listed=[...serial.matchAll(/Route (\d): ([0-9.]+),([0-9.]+)/g)];
  assert.equal(listed.length,4,'all waypoints reported before movement');
  for(let i=0;i<4;i++) for(let j=0;j<2;j++) assert(Math.abs(Number(listed[i][j+2])-expectedRoute[i][j])<.003);
  until(()=>serial.includes(carry?'Piece motion complete;':'Board motion complete.'),12000);
  run(80); at(to);
  assert.deepEqual([...serial.matchAll(/Waypoint (\d)\/3/g)].map(x=>Number(x[1])),[0,1,2,3]);
  if(carry) {
    assert.equal(relayEvents.length,2); assert(relayEvents[0].on&&!relayEvents[1].on);
    for(let i=0;i<2;i++) {
      assert(Math.abs(relayEvents[0].physical[i]*.0075-expectedRoute[0][i])<.04,'pickup at source');
      assert(Math.abs(relayEvents[1].physical[i]*.0075-expectedRoute[3][i])<.04,'release at destination');
      assert((piecePulses[i][0].at-relayEvents[0].at)/16000>=349,'pickup settling');
      const minGap=piecePulses[i].slice(1).reduce((a,p,n)=>Math.min(a,(p.at-piecePulses[i][n].at)/16),Infinity);
      const peak=1e6/minGap;
      const isLong=Math.abs(expectedRoute[3][i]-expectedRoute[0][i])>40;
      const expectedRate=isLong?10000:10000/Math.sqrt(2);
      assert(Math.abs(peak-expectedRate)<180,'75 mm/s path-speed scaling: '+peak);
    }
  } else assert.equal(relayEvents.length,0,'dry run never energizes');
  assert(!relayHigh);
  trackPiece=false;
  command('?',120); assert(serial.includes('Reference ready;'), 'successful knight retains reference: '+serial);
  console.log('PASS '+(carry?'carry ':'dry ')+from+' -> '+to+'; max path error '+maxCrossTrack.toFixed(3)+' mm');
}

allLimitsReleased(); run(200);
assert.deepEqual(counts(),[0,0]); assert(!relayHigh);
assert(serial.includes('board/knight/parking tests'));
assert(serial.includes('BOOT: carriage unreferenced; piece map unknown.'));
assert(serial.includes('Travel/piece mm/s: 150.0/75.0'));
const startup=counts();
for(const removed of ['1','2','+','-','b','a','m','c']) {
  command(removed+'\n'); assert.deepEqual(counts(),startup); assert(!relayHigh);
}
for(const malformed of ['g','ga0','gi1','ga1a','k','kb1','kb1c9','kb1c3a','k'+ 'a'.repeat(80),'kb1b2','kb1b1',
  'p','pa132','pa101a','ud400','i321','ia12','ia111']) {
  command(malformed+'\n'); assert(/Invalid/.test(serial)); assert.deepEqual(counts(),startup);
}
command('nwgA1\nkb1c3\n'); assert(serial.includes('Board unavailable'));
assert.deepEqual(counts(),startup); assert(!relayHigh);
console.log('PASS startup 150/75, removed commands inert, malformed/illegal input rejected, home required');
modelEnabled=true; updateModel();

if (badStorage) {
  command('r'); assert(serial.includes('No saved geometry')); assert.deepEqual(counts(), startup);
  console.log('PASS damaged/incomplete/config-incompatible EEPROM cannot authorize saved reference');
} else if (restoreTest) {
  command('q',450); assert.equal([...serial.matchAll(/^P\d\d,/gm)].length,32);
  const before=counts(); command('ga1\n'); assert(serial.includes('Board unavailable')); assert.deepEqual(counts(),before);
  home('r');
  for(let i=0;i<2;i++) assert(counts()[i]-before[i]<20000, 'MIN-only home avoids traversing to MAX');
  boardMove('ga1\n'); command('?',120); assert(serial.includes('Reference ready;')); assert(serial.includes('map unknown'));
  console.log('PASS EEPROM reboot restores geometry only; short MIN reference re-enables motion');
} else if(storageTest) {
  command('r'); assert(serial.includes('No saved geometry')); assert.deepEqual(counts(),startup);
  home(); command('s',300); assert(serial.includes('Geometry saved to EEPROM.'));
  assert.equal(eeprom.memory[0],0xa7);
  fs.writeFileSync(eepromFile,eeprom.memory);
  command('f'); runKnight('b1','c3',true);
  command('o'); assert(serial.includes('idle reference retained'));
  boardMove('ga1\n'); command('q',450); assert.equal([...serial.matchAll(/^P\d\d,/gm)].length,32);
  console.log('PASS explicit EEPROM save and idle o preserve geometry/reference after carry');
} else if(fullBoardTest) {
  home();
  command('kb1c3\n'); assert(serial.includes('Map rejects move'));
  command('f');
  const pairs=[['b1','a3'],['b1','c3'],['g1','f3'],['g1','h3'],['b8','a6'],['b8','c6'],['g8','f6'],['g8','h6'],
    ['a1',0],['h1',8],['a8',9],['h8',17],['a2',18],['a7',24],['h2',25],['h7',31],['d1',4],['d8',13]];
  const parks = () => {
    command('q',450);
    return [...serial.matchAll(/^P(\d\d),([0-9.]+),([0-9.]+)/gm)].map(m=>[+m[2],+m[3]]);
  };
  const parkXY=parks(); assert.equal(parkXY.length,32); assert.equal(new Set(parkXY.map(p=>p.join(','))).size,32);
  const occupied=new Set([...Array(16).keys(),...Array.from({length:16},(_,i)=>48+i)]);
  const id = name => typeof name==='number'?64+name:(Number(name[1])-1)*8+name.charCodeAt(0)-97;
  function checkMap() {
    command('v',80);
    const rows=serial.match(/^[01]{8}\r?$/gm).map(row=>row.trim());
    assert.equal(rows.length,8);
    for(let r=0;r<8;r++) for(let f=0;f<8;f++) assert.equal(rows[7-r][f],occupied.has(r*8+f)?'1':'0');
    const slots=serial.match(/Parking 00\.\.31: ([01]{32})/)[1];
    for(let p=0;p<32;p++) assert.equal(slots[p],occupied.has(p+64)?'1':'0');
  }
  checkMap();
  const rejected=counts(); command('kb1d2\n'); assert(serial.includes('Map rejects move'));
  command('pd400\n'); assert(serial.includes('Invalid parking')); assert.deepEqual(counts(),rejected);
  let leg=0;
  for(const pair of pairs) for(let reverse=0;reverse<2;reverse++) {
    const [from,to]=reverse?[...pair].reverse():pair;
    expectedRoute=typeof pair[1]==='number'?[squareXY(pair[0]),parkXY[pair[1]]]:routeFor(...pair);
    if(reverse) expectedRoute.reverse();
    trackPiece=true; relayEvents=[]; maxCrossTrack=0;
    command('j'); assert(serial.includes('Fixture leg '+(++leg)+'/36'));
    until(()=>serial.includes('Piece motion complete;'),12000); run(80);
    assert.equal(relayEvents.length,2);
    assert(relayEvents[0].on&&!relayEvents[1].on);
    for(let i=0;i<2;i++) {
      assert(Math.abs(relayEvents[0].physical[i]*.0075-expectedRoute[0][i])<.04);
      assert(Math.abs(relayEvents[1].physical[i]*.0075-expectedRoute.at(-1)[i])<.04);
    }
    trackPiece=false; occupied.delete(id(from)); occupied.add(id(to)); checkMap();
    command('o?',120); assert(serial.includes('Reference ready;'), serial);
    console.log('PASS fixture '+leg+'/36 '+from+' -> '+to+'; map committed; reference retained');
  }
  command('j'); assert(serial.includes('Fixture unavailable/done'));
  command('pa100\n'); expectedRoute=[squareXY('a1'),parkXY[0]]; trackPiece=true;
  until(()=>serial.includes('Piece motion complete;'),12000); run(80); trackPiece=false;
  command('ia11\n'); const before=counts(); command('pa100\n'); assert(serial.includes('Map rejects move')); assert.deepEqual(counts(),before);
  command('ia10\n'); expectedRoute=[parkXY[0],squareXY('a1')]; trackPiece=true;
  command('ua100\n'); until(()=>serial.includes('Piece motion complete;'),12000); run(80); trackPiece=false;
  console.log('PASS manual parking/retrieval, occupancy edits and occupied-slot rejection');
} else if(smallTableTest) {
  command('h'); until(()=>serial.includes('Board centers do not fit'),100000); run(60);
  const before=counts(); command('nkb1c3\n');
  assert(serial.includes('Board unavailable')); assert.deepEqual(counts(),before);
  console.log('PASS undersized table cannot enable board/knight moves or shrink pitch');
} else if(faultTest) {
  home();
  command('f');
  expectedRoute=routeFor('b1','c3'); trackPiece=true;
  command('kb1c3\n'); until(()=>relayHigh,6000); run(430);
  // x works halfway through another command, while the current move carries a piece.
  send('kd4ex'); run(5); assertStopped();
  console.log('PASS x during loaded motion/partial line cancels both queues and references');
  home(); expectedRoute=routeFor('b1','c3'); trackPiece=true;
  command('f');
  command('kb1c3\n'); until(()=>relayHigh,6000); run(430);
  modelEnabled=false; setLimit(1,'max',true); run(5); assertStopped();
  console.log('PASS any limit during loaded motion stops both axes and drops magnet');
  modelEnabled=true; modelFault='stuck-min'; physical[0]=0; updateModel(); trackBoard=false; trackPiece=false;
  command('h'); until(()=>serial.includes('Calibration failed:'),4000); run(60);
  assert(serial.includes('MIN did not release')); assert(!relayHigh);
  console.log('PASS stuck switch bounded homing failure');
  modelFault='missing-max'; physical[0]=100; updateModel();
  command('h'); until(()=>serial.includes('Calibration failed:'),25000); run(60);
  assert(serial.includes('Limit not reached within search distance')); assert(!relayHigh);
  console.log('PASS missing MAX bounded search failure');
  modelEnabled=false; allLimitsReleased(); setLimit(0,'min',true); setLimit(0,'max',true);
  const rejected=counts(); command('h'); assert(serial.includes('Homing rejected')); assert.deepEqual(counts(),rejected);
  allLimitsReleased(); modelFault=''; modelEnabled=true; updateModel();
  command('h',100); send('gax'); run(5); assertStopped();
  console.log('PASS both-limits rejection and homing cancellation');
} else {
  home();
  command('q',350);
  const grid=[...serial.matchAll(/^([a-h][1-8]),([0-9.]+),([0-9.]+)/gm)];
  assert.equal(grid.length,64); assert.equal(new Set(grid.map(x=>x[1])).size,64);
  boardMove('n'); at('a1'); boardMove('n'); at('b1');
  boardMove('gH8\r\n'); at('h8'); boardMove('gh8\n'); at('h8');
  console.log('PASS measured grid, next square, named/zero-distance travel');
  const beforeWalk=counts(); command('w');
  // Attempts while active cannot replace this route or start homing.
  send('kb1c3\nh'); run(50); assert(serial.includes('Busy; wait or x.'));
  until(()=>serial.includes('Board motion complete.'),65000); run(30);
  const visited=[...serial.matchAll(/At ([a-h][1-8])/g)].map(x=>x[1]);
  assert.deepEqual(visited,Array.from({length:64},(_,i)=>
    String.fromCharCode(97+(Math.floor(i/8)%2?7-i%8:i%8))+(Math.floor(i/8)+1)));
  at('a8');
  const walk=axes[0].pulses.slice(beforeWalk[0]);
  const rate=1e6/walk.slice(1).reduce((v,p,n)=>Math.min(v,(p.at-walk[n].at)/16),Infinity);
  assert(rate>19800&&rate<20200,'150 mm/s travel preserved');
  console.log('PASS full 64-square walk, 150 mm/s travel, bounds and ownership');
  runKnight('b1','c3',false,true);
  const stationary=counts();
  for(const invalid of ['kd4d4','kd4e5','dh8h9','kd4e6aaaa']) {
    command(invalid+'\n'); assert(serial.includes('Invalid')); assert.deepEqual(counts(),stationary);
  }
  for(const to of ['c2','e2','b3','f3','b5','f5','c6','e6']) runKnight('d4',to,true,true);
  runKnight('e6','d4',true);
  for(const [from,to] of [['a1','b3'],['h1','f2'],['a8','c7'],['h8','g6']]) runKnight(from,to,false);
  expectedRoute=routeFor('b1','c3'); trackPiece=true;
  command('f');
  command('kb1c3\n'); until(()=>relayHigh,6000);
  // o cancels even inside an overflowing input and during the pickup dwell.
  send('k'+'a'.repeat(15)+'o'); run(5); assertStopped();
  console.log('PASS immediate o during pickup/overflow drops magnet and requires fresh homing');
}
for(let i=0;i<2;i++) console.log('Axis '+i+': minimum STEP high '+axes[i].minHigh+' us, low '+axes[i].minLow+' us; DIR setup '+axes[i].minDir+' us');
console.log('PASS compiled Uno firmware; physical clearance and magnetic coupling require bench testing.');
