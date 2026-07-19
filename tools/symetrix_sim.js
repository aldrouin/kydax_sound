#!/usr/bin/env node
/**
 * Symetrix Jupiter appliance simulator (see PROTOCOL.md).
 *
 * Listens on UDP port 48630 (or the port given as first argument) and speaks
 * the Jupiter control protocol so the kydax_sound integration can be tested
 * without the real appliance:
 *
 *   node tools/symetrix_sim.js [port]
 *
 * Behavior notes:
 * - Permissive: every controller number 1-10000 "exists" (value 0 until
 *   written). The 8 restaurant zone channels are pre-seeded at -20 dB.
 * - Push (PUE/PUR/...) is implemented: enabled controllers push value
 *   changes to the last client that sent a command, batched every 100 ms.
 * - Every command and response is logged so you can watch the traffic.
 */

const dgram = require("dgram");

const PORT = Number(process.argv[2] || 48630);
const CR = "\r";

const state = new Map(); // controller -> position (0-65535)
let preset = 0;
let lastClient = null; // {address, port} of the last sender
const pushEnabled = new Set();
let pushGlobal = true;
const pushQueue = new Map(); // controller -> position, flushed every 100 ms

// Seed the 8 zone volume channels of the restaurant at -20 dB.
const DB_MIN = -72, DB_MAX = 12;
const dbToPos = (db) =>
  Math.round(((db - DB_MIN) / (DB_MAX - DB_MIN)) * 65535);
for (let ch = 7122; ch <= 7164; ch += 6) state.set(ch, dbToPos(-20));

const pad = (n, width) => {
  const negative = n < 0;
  const digits = String(Math.abs(n)).padStart(negative ? width - 1 : width, "0");
  return (negative ? "-" : "") + digits;
};
const posToDb = (pos) => DB_MIN + (DB_MAX - DB_MIN) * (pos / 65535);
const valid = (ch) => Number.isInteger(ch) && ch >= 1 && ch <= 10000;
const get = (ch) => (state.has(ch) ? state.get(ch) : 0);

function set(ch, pos) {
  state.set(ch, pos);
  if (pushGlobal && pushEnabled.has(ch)) pushQueue.set(ch, pos);
}

function rangeArgs(tokens, fallbackLow = 1, fallbackHigh = 10000) {
  const low = tokens.length > 1 ? Number(tokens[1]) : fallbackLow;
  const high = tokens.length > 2 ? Number(tokens[2]) : tokens.length > 1 ? low : fallbackHigh;
  return [low, high];
}

function handle(command) {
  const tokens = command.trim().split(/\s+/);
  const cmd = (tokens[0] || "").toUpperCase();

  switch (cmd) {
    case "CS": {
      const ch = Number(tokens[1]), pos = Number(tokens[2]);
      if (!valid(ch) || !(pos >= 0 && pos <= 65535)) return "NAK";
      set(ch, pos);
      console.log(`    channel ${ch} -> ${pos} (${posToDb(pos).toFixed(1)} dB)`);
      return "ACK";
    }
    case "CC": {
      const ch = Number(tokens[1]), inc = Number(tokens[2]), amount = Number(tokens[3]);
      if (!valid(ch) || !(inc === 0 || inc === 1) || !(amount >= 0 && amount <= 65535))
        return "NAK";
      const next = inc === 1 ? Math.min(65535, get(ch) + amount) : Math.max(0, get(ch) - amount);
      set(ch, next);
      return "ACK";
    }
    case "GS": {
      const ch = Number(tokens[1]);
      return valid(ch) ? String(get(ch)) : "NAK";
    }
    case "GS2": {
      const ch = Number(tokens[1]);
      return valid(ch) ? `${ch} ${get(ch)}` : "NAK";
    }
    case "GSB": {
      const start = Number(tokens[1]), count = Number(tokens[2]);
      if (!valid(start) || !(count >= 1 && count <= 256)) return "NAK";
      const lines = [];
      for (let i = 0; i < count; i++)
        lines.push(valid(start + i) ? pad(get(start + i), 5) : "-0001");
      return lines.join(CR);
    }
    case "GSB2": {
      const start = Number(tokens[1]), count = Number(tokens[2]);
      if (!valid(start) || !(count >= 1 && count <= 256)) return "NAK";
      const lines = [];
      for (let i = 0; i < count; i++) {
        const value = valid(start + i) ? pad(get(start + i), 5) : "-0001";
        lines.push(`#${pad(start + i, 5)}=${value}`);
      }
      return lines.join(CR);
    }
    case "LP": {
      const n = Number(tokens[1]);
      if (!(n >= 1 && n <= 150)) return "NAK";
      preset = n;
      console.log(`    preset ${n} loaded`);
      return "ACK";
    }
    case "GPR":
      return tokens[1] === "D" ? `PrstD=${pad(preset, 4)}` : "NAK";
    case "FU":
      console.log("    *** front panel LEDs flash ***");
      return "ACK";
    case "SQ":
    case "EH":
    case "PUI":
    case "PUT":
      return "ACK";
    case "PU":
      pushGlobal = tokens[1] === "1";
      return "ACK";
    case "PUE": {
      const [low, high] = rangeArgs(tokens);
      for (let ch = low; ch <= high; ch++) if (valid(ch)) pushEnabled.add(ch);
      return "ACK";
    }
    case "PUD": {
      const [low, high] = rangeArgs(tokens);
      for (let ch = low; ch <= high; ch++) pushEnabled.delete(ch);
      return "ACK";
    }
    case "PUC":
      pushQueue.clear();
      return "ACK";
    case "PUR": {
      const [low, high] = rangeArgs(tokens);
      for (const ch of pushEnabled)
        if (ch >= low && ch <= high) pushQueue.set(ch, get(ch));
      return "ACK";
    }
    case "GPU":
      if (tokens[1] === "0")
        return `Global=${pushGlobal ? 1 : 0}${CR}00001 10000 00001 00001 00103`;
      return [...pushEnabled].sort((a, b) => a - b).join(CR) || "ACK";
    default:
      return "NAK";
  }
}

const socket = dgram.createSocket("udp4");

socket.on("message", (data, rinfo) => {
  lastClient = { address: rinfo.address, port: rinfo.port };
  const command = data.toString("ascii").replace(/[\r\n\0]+$/, "");
  const response = handle(command);
  console.log(`[${rinfo.address}:${rinfo.port}] ${command}  ->  ${response.replace(/\r/g, " | ")}`);
  socket.send(response + CR, rinfo.port, rinfo.address);
});

setInterval(() => {
  if (!pushGlobal || pushQueue.size === 0 || lastClient === null) return;
  const lines = [...pushQueue.entries()]
    .slice(0, 64)
    .map(([ch, pos]) => `#${pad(ch, 5)}=${pad(pos, 5)}`);
  for (const [ch] of [...pushQueue.entries()].slice(0, 64)) pushQueue.delete(ch);
  const packet = lines.join(CR) + CR;
  console.log(`push -> ${lastClient.address}:${lastClient.port}: ${lines.join(" | ")}`);
  socket.send(packet, lastClient.port, lastClient.address);
}, 100);

socket.bind(PORT, () => {
  console.log(`Symetrix Jupiter simulator listening on UDP ${PORT}`);
  console.log(`Zone channels 7122-7164 (step 6) seeded at -20 dB (${dbToPos(-20)})`);
});
