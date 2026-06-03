// Hand-rolled STORE-method zip writer (no compression, no deps). The Phase 3
// STL-set export needs to download N STLs + a print-guide as a single file;
// the browser has no native zip API and the v2 plan forbids new deps. STORE
// is acceptable because STL binaries don't compress well anyway and the
// print-guide is small.
//
// Spec reference: APPNOTE 6.3.6, sections 4.3 (local file header), 4.4
// (central directory), 4.5 (end of central dir record).

const TEXT_ENCODER = new TextEncoder();

// CRC32 with the standard reversed polynomial 0xEDB88320. Each zip entry
// stores the CRC of its uncompressed payload; readers verify it.
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[i] = c;
  }
  return table;
})();

function crc32(data) {
  let crc = 0xffffffff;
  for (let i = 0; i < data.length; i++) {
    crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function toBytes(payload) {
  if (payload instanceof Uint8Array) return payload;
  if (typeof payload === 'string') return TEXT_ENCODER.encode(payload);
  if (payload instanceof ArrayBuffer) return new Uint8Array(payload);
  throw new TypeError(`zip: unsupported payload type ${typeof payload}`);
}

/**
 * Build a Uint8Array zip from {filename: Uint8Array|string} entries.
 * Uses STORE (no compression) so the entire output is one pass over the
 * inputs.
 */
export function buildZip(entries) {
  const chunks = [];
  const centralDir = [];
  let offset = 0;

  for (const [name, payload] of Object.entries(entries)) {
    const nameBytes = TEXT_ENCODER.encode(name);
    const data = toBytes(payload);
    const crc = crc32(data);

    const localHeader = new Uint8Array(30 + nameBytes.length);
    const lhView = new DataView(localHeader.buffer);
    lhView.setUint32(0, 0x04034b50, true);  // local file header signature
    lhView.setUint16(4, 20, true);           // version needed (2.0)
    lhView.setUint16(6, 0, true);            // general purpose bit flag
    lhView.setUint16(8, 0, true);            // compression method = STORE
    lhView.setUint16(10, 0, true);           // last mod time
    lhView.setUint16(12, 0x21, true);        // last mod date (1980-01-01 placeholder)
    lhView.setUint32(14, crc, true);
    lhView.setUint32(18, data.length, true); // compressed size
    lhView.setUint32(22, data.length, true); // uncompressed size
    lhView.setUint16(26, nameBytes.length, true);
    lhView.setUint16(28, 0, true);           // extra field length
    localHeader.set(nameBytes, 30);

    chunks.push(localHeader, data);

    const cdEntry = new Uint8Array(46 + nameBytes.length);
    const cdView = new DataView(cdEntry.buffer);
    cdView.setUint32(0, 0x02014b50, true);   // central directory signature
    cdView.setUint16(4, 20, true);           // version made by
    cdView.setUint16(6, 20, true);           // version needed
    cdView.setUint16(8, 0, true);
    cdView.setUint16(10, 0, true);
    cdView.setUint16(12, 0, true);
    cdView.setUint16(14, 0x21, true);
    cdView.setUint32(16, crc, true);
    cdView.setUint32(20, data.length, true);
    cdView.setUint32(24, data.length, true);
    cdView.setUint16(28, nameBytes.length, true);
    cdView.setUint16(30, 0, true);           // extra field length
    cdView.setUint16(32, 0, true);           // comment length
    cdView.setUint16(34, 0, true);           // disk number start
    cdView.setUint16(36, 0, true);           // internal attrs
    cdView.setUint32(38, 0, true);           // external attrs
    cdView.setUint32(42, offset, true);      // local header offset
    cdEntry.set(nameBytes, 46);
    centralDir.push(cdEntry);

    offset += localHeader.length + data.length;
  }

  const cdSize = centralDir.reduce((sum, c) => sum + c.length, 0);
  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  eocdView.setUint32(0, 0x06054b50, true);   // EOCD signature
  eocdView.setUint16(4, 0, true);            // disk number
  eocdView.setUint16(6, 0, true);            // disk where CD starts
  eocdView.setUint16(8, centralDir.length, true);   // entries on this disk
  eocdView.setUint16(10, centralDir.length, true);  // total entries
  eocdView.setUint32(12, cdSize, true);
  eocdView.setUint32(16, offset, true);      // offset of central directory
  eocdView.setUint16(20, 0, true);           // comment length

  const totalSize = offset + cdSize + 22;
  const out = new Uint8Array(totalSize);
  let pos = 0;
  for (const chunk of chunks) {
    out.set(chunk, pos);
    pos += chunk.length;
  }
  for (const entry of centralDir) {
    out.set(entry, pos);
    pos += entry.length;
  }
  out.set(eocd, pos);
  return out;
}
