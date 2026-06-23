'use strict';

/**
 * Minimal zero-dependency ZIP writer.
 * ───────────────────────────────────
 * Streams a directory tree to a writable stream (e.g. an http response) as a
 * standard DEFLATE-compressed .zip — double-clickable on Windows/macOS/Linux.
 *
 * We hand-roll the ZIP format (local headers + central directory + EOCD) using
 * Node's built-in `zlib`, so the desktop build doesn't need an extra npm dep
 * like `archiver`. Each file is deflated in memory, which is fine for the
 * file-manager use case; very large files (>4 GB) are not supported (no zip64).
 */

const fs   = require('fs');
const path = require('path');
const zlib = require('zlib');

// ── CRC32 ──────────────────────────────────────────────────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// ── Directory walk (files only, posix-style relative paths) ──────────────────
function walk(dir, base, out) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
  catch { return; }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    const rel  = base ? base + '/' + e.name : e.name;
    if (e.isDirectory()) walk(full, rel, out);
    else if (e.isFile())  out.push({ full, rel });
  }
}

function dosDateTime(d) {
  const time = ((d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1)) & 0xFFFF;
  const date = (((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate()) & 0xFFFF;
  return { time, date };
}

/**
 * Write `rootDir` as a zip to the writable `out`. Returns the number of files.
 * Synchronous deflate per file; writes are streamed so memory stays bounded to
 * one file at a time.
 */
function zipDirToStream(rootDir, out) {
  const files = [];
  walk(rootDir, '', files);

  const central = [];
  let offset = 0;
  const write = buf => { out.write(buf); offset += buf.length; };

  for (const f of files) {
    let raw;
    try { raw = fs.readFileSync(f.full); } catch { continue; }
    const crc      = crc32(raw);
    const comp     = zlib.deflateRawSync(raw);
    const nameBuf  = Buffer.from(f.rel, 'utf8');
    let mtime;
    try { mtime = fs.statSync(f.full).mtime; } catch { mtime = new Date(); }
    const { time, date } = dosDateTime(mtime);
    const localOffset = offset;

    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0);   // local file header signature
    lh.writeUInt16LE(20, 4);           // version needed
    lh.writeUInt16LE(0x0800, 6);       // flags: UTF-8 filename
    lh.writeUInt16LE(8, 8);            // method: deflate
    lh.writeUInt16LE(time, 10);
    lh.writeUInt16LE(date, 12);
    lh.writeUInt32LE(crc, 14);
    lh.writeUInt32LE(comp.length, 18); // compressed size
    lh.writeUInt32LE(raw.length, 22);  // uncompressed size
    lh.writeUInt16LE(nameBuf.length, 26);
    lh.writeUInt16LE(0, 28);           // extra length
    write(lh);
    write(nameBuf);
    write(comp);

    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0);   // central directory header signature
    ch.writeUInt16LE(20, 4);           // version made by
    ch.writeUInt16LE(20, 6);           // version needed
    ch.writeUInt16LE(0x0800, 8);       // flags: UTF-8
    ch.writeUInt16LE(8, 10);           // method: deflate
    ch.writeUInt16LE(time, 12);
    ch.writeUInt16LE(date, 14);
    ch.writeUInt32LE(crc, 16);
    ch.writeUInt32LE(comp.length, 20);
    ch.writeUInt32LE(raw.length, 24);
    ch.writeUInt16LE(nameBuf.length, 28);
    ch.writeUInt16LE(0, 30);           // extra length
    ch.writeUInt16LE(0, 32);           // comment length
    ch.writeUInt16LE(0, 34);           // disk number start
    ch.writeUInt16LE(0, 36);           // internal attrs
    ch.writeUInt32LE(0, 38);           // external attrs
    ch.writeUInt32LE(localOffset, 42); // offset of local header
    central.push(Buffer.concat([ch, nameBuf]));
  }

  const cdStart = offset;
  for (const c of central) write(c);
  const cdSize = offset - cdStart;

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);   // EOCD signature
  eocd.writeUInt16LE(0, 4);            // disk number
  eocd.writeUInt16LE(0, 6);            // disk with central dir
  eocd.writeUInt16LE(central.length, 8);
  eocd.writeUInt16LE(central.length, 10);
  eocd.writeUInt32LE(cdSize, 12);
  eocd.writeUInt32LE(cdStart, 16);
  eocd.writeUInt16LE(0, 20);           // comment length
  write(eocd);

  return files.length;
}

module.exports = { zipDirToStream };
