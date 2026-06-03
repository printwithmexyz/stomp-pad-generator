// Sync runtime assets into public/ so the same-origin fetches at runtime work.
//   1. stomppad/ Python package (single source of truth in the parent project),
//      bundled as a JSON manifest that main.js unpacks into Pyodide's FS.
//   2. openscad-wasm (downloaded from files.openscad.org, unzipped, cached).
// Runs automatically before `dev` and `build`; can also be invoked via `npm run prepare`.

import {
  mkdirSync, existsSync, writeFileSync, readFileSync,
  readdirSync, lstatSync, rmSync, unlinkSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, relative } from 'node:path';
import extract from 'extract-zip';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const publicDir = resolve(root, 'public');

mkdirSync(publicDir, { recursive: true });

// --- 1. stomppad manifest ----------------------------------------------------
// Walk stomppad/ and emit a JSON manifest with each .py file inlined as a
// string. main.js fetches the manifest once at boot and writes each entry to
// Pyodide's virtual FS. Phase 1 will add submodules (geometry, packing,
// patterns, project); this walk picks them up automatically.
//
// Why a manifest and not a real .zip: Node has no built-in zip writer, and
// the v2 plan called for "no new dep". Pyodide can `unpackArchive` a real
// zip if/when we add binary assets; for an all-text package, a manifest is
// the smaller, debuggable choice and keeps prepare.js dependency-free.
const stomppadSrc = resolve(root, '..', 'stomppad');
const manifestDest = resolve(publicDir, 'stomppad-manifest.json');
if (!existsSync(stomppadSrc)) {
  console.error(`prepare.js: stomppad package not found at ${stomppadSrc}`);
  process.exit(1);
}

function walk(dir, base = dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === '__pycache__') continue;
    const full = resolve(dir, name);
    // lstat, not stat: never recurse through a symlink. A symlinked subdir
    // could loop back into the package and produce an unbounded manifest.
    const st = lstatSync(full);
    if (st.isSymbolicLink()) continue;
    if (st.isDirectory()) {
      out.push(...walk(full, base));
    } else if (name.endsWith('.py')) {
      out.push({
        path: relative(base, full).replaceAll('\\', '/'),
        content: readFileSync(full, 'utf8'),
      });
    }
  }
  return out;
}

const files = walk(stomppadSrc);
writeFileSync(manifestDest, JSON.stringify({ files }));
console.log(`prepare.js: synced stomppad/ (${files.length} files) -> ${manifestDest}`);

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
