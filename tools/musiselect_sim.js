#!/usr/bin/env node
/**
 * MusiSelect device simulator (see PROTOCOL.md).
 *
 * The real device's response behavior is unknown (commands were historically
 * fire-and-forget), so this simply listens on UDP port 2325 (or the port
 * given as first argument) and logs every command received:
 *
 *   node tools/musiselect_sim.js [port]
 */

const dgram = require("dgram");

const PORT = Number(process.argv[2] || 2325);
const socket = dgram.createSocket("udp4");

socket.on("message", (data, rinfo) => {
  const command = data.toString("ascii").replace(/[\r\n\0]+$/, "");
  console.log(`[${rinfo.address}:${rinfo.port}] ${command}`);
});

socket.bind(PORT, () => {
  console.log(`MusiSelect simulator listening on UDP ${PORT} (log only)`);
});
