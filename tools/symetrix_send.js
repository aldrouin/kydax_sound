#!/usr/bin/env node
/**
 * Send one command to a Symetrix Jupiter (or the simulator) and print the
 * response:
 *
 *   node tools/symetrix_send.js "GS 7122" [host] [port]
 *
 * Defaults: host 127.0.0.1, port 48630.
 */

const dgram = require("dgram");

const command = process.argv[2];
if (!command) {
  console.error('usage: node symetrix_send.js "<command>" [host] [port]');
  process.exit(2);
}
const host = process.argv[3] || "127.0.0.1";
const port = Number(process.argv[4] || 48630);

const socket = dgram.createSocket("udp4");
const timer = setTimeout(() => {
  console.error("timeout: no response after 2 s");
  socket.close();
  process.exit(1);
}, 2000);

socket.on("message", (data) => {
  clearTimeout(timer);
  console.log(data.toString("ascii").replace(/\r/g, "\n").trimEnd());
  socket.close();
});

socket.send(command + "\r", port, host);
