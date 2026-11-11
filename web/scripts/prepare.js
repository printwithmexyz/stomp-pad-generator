// Sync runtime assets into public/ so the same-origin fetches at runtime work.
//   1. The stomppad geometry package (single source of truth in the parent project).
//   2. openscad-wasm (downloaded from files.openscad.org, unzipped, cached).
// Runs automatically before `dev` and `build`; can also be invoked via `npm run prepare`.

import {
  copyFileSync, mkdirSync, existsSync, writeFileSync, readFileSync,
  readdirSync, rmSync, unlinkSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import extract from 'extract-zip';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const publicDir = resolve(root, 'public');

mkdirSync(publicDir, { recursive: true });

// --- 1. geometry package -----------------------------------------------------
// Pyodide can't import a package straight off a web server. Copy every module
// next to a manifest that main.js walks to rebuild the package in Pyodide's FS.
const packageSrc = resolve(root, '..', 'stomppad');
const packageDest = resolve(publicDir, 'stomppad');
if (!existsSync(packageSrc)) {
  console.error(`prepare.js: stomppad package not found at ${packageSrc}`);
  process.exit(1);
}
rmSync(packageDest, { recursive: true, force: true });
mkdirSync(packageDest);
const packageModules = readdirSync(packageSrc).filter((name) => name.endsWith('.py')).sort();
for (const name of packageModules) {
  copyFileSync(resolve(packageSrc, name), resolve(packageDest, name));
}
writeFileSync(resolve(packageDest, 'manifest.json'), JSON.stringify(packageModules));
rmSync(resolve(publicDir, 'pyramid_position_calculator.py'), { force: true });
console.log(`prepare.js: synced ${packageModules.length} stomppad modules -> ${packageDest}`);

// --- 2. openscad-wasm --------------------------------------------------------
// We use the official OpenSCAD playground build (manifold backend, 2025+)
// rather than the openscad/openscad-wasm GitHub releases (last tagged
// 2022.03.20, CGAL-only, asserts on geometry the manifold backend handles).
const OPENSCAD_VERSION = '2025.03.25';
const OPENSCAD_URL = `https://files.openscad.org/playground/OpenSCAD-${OPENSCAD_VERSION}.wasm24456-WebAssembly-web.zip`;
const openscadDir = resolve(publicDir, 'openscad');
const versionFile = resolve(openscadDir, 'VERSION');

const installedVersion = existsSync(versionFile)
  ? readFileSync(versionFile, 'utf8').trim()
  : null;

if (installedVersion === OPENSCAD_VERSION) {
  console.log(`prepare.js: openscad-wasm ${OPENSCAD_VERSION} already present`);
} else {
  if (installedVersion) {
    console.log(`prepare.js: replacing openscad-wasm ${installedVersion} with ${OPENSCAD_VERSION}`);
  }
  rmSync(openscadDir, { recursive: true, force: true });
  mkdirSync(openscadDir, { recursive: true });

  const zipPath = resolve(publicDir, '_openscad.zip');
  console.log(`prepare.js: downloading ${OPENSCAD_URL}`);
  const resp = await fetch(OPENSCAD_URL);
  if (!resp.ok) {
    console.error(`prepare.js: failed to download: HTTP ${resp.status}`);
    process.exit(1);
  }
  const data = new Uint8Array(await resp.arrayBuffer());
  writeFileSync(zipPath, data);
  console.log(`prepare.js: extracting (${(data.length / 1024 / 1024).toFixed(1)} MB)`);
  await extract(zipPath, { dir: openscadDir });
  unlinkSync(zipPath);
  writeFileSync(versionFile, OPENSCAD_VERSION);
  console.log(`prepare.js: openscad-wasm ${OPENSCAD_VERSION} ready`);
}
