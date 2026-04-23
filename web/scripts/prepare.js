// Sync runtime assets into public/ so the same-origin fetches at runtime work.
//   1. Calculator module (single source of truth in the parent project).
//   2. openscad-wasm release files (downloaded once, cached on disk).
// Runs automatically before `dev` and `build`; can also be invoked via `npm run prepare`.

import { copyFileSync, mkdirSync, existsSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const publicDir = resolve(root, 'public');

mkdirSync(publicDir, { recursive: true });

// --- 1. calculator -----------------------------------------------------------
const calcSrc = resolve(root, '..', 'pyramid_position_calculator.py');
const calcDest = resolve(publicDir, 'pyramid_position_calculator.py');
if (!existsSync(calcSrc)) {
  console.error(`prepare.js: calculator source not found at ${calcSrc}`);
  process.exit(1);
}
copyFileSync(calcSrc, calcDest);
console.log(`prepare.js: synced calculator -> ${calcDest}`);

// --- 2. openscad-wasm --------------------------------------------------------
const OPENSCAD_VERSION = '2022.03.20';
const OPENSCAD_FILES = ['openscad.js', 'openscad.wasm', 'openscad.wasm.js'];
const releaseBase = `https://github.com/openscad/openscad-wasm/releases/download/${OPENSCAD_VERSION}`;
const openscadDir = resolve(publicDir, 'openscad');
mkdirSync(openscadDir, { recursive: true });

for (const file of OPENSCAD_FILES) {
  const dest = resolve(openscadDir, file);
  if (existsSync(dest)) {
    console.log(`prepare.js: ${file} already present, skipping`);
    continue;
  }
  const url = `${releaseBase}/${file}`;
  console.log(`prepare.js: downloading ${url}`);
  const resp = await fetch(url);
  if (!resp.ok) {
    console.error(`prepare.js: failed to download ${file}: HTTP ${resp.status}`);
    process.exit(1);
  }
  const data = new Uint8Array(await resp.arrayBuffer());
  writeFileSync(dest, data);
  console.log(`prepare.js: wrote ${dest} (${(data.length / 1024).toFixed(1)} KB)`);
}
