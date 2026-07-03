#!/usr/bin/env node
/*
 * Python ↔ JS parity check.
 *
 * Runs the Python CLI and the JS (browser) pipeline on the fixtures in
 * tests/, in both modes:
 *
 *   1. verified   — Canvas .imscc + edX .tar.gz
 *   2. unverified — Canvas .imscc + --target course ID
 *
 * and asserts that the two implementations produce:
 *
 *   - the same set of entries in the updated .imscc, with identical
 *     decompressed bytes per entry (raw zip bytes differ because Python's
 *     zipfile and zip.js compress differently — content is what matters), and
 *   - byte-identical audit CSVs.
 *
 * Any change to matching/rewriting logic must be made in BOTH pipelines
 * (src/ and lti-core.js/stream-core.js); this script is the guard rail.
 *
 * Usage:  npm install && npm test        (Node 18+, Python 3.9+)
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const TESTS = path.join(ROOT, 'tests');
const IMSCC = path.join(TESTS, 'kalebs-mitx-canvas-testing-site-export.imscc');
const TARGZ = path.join(TESTS, 'course.260611115550.tar.gz');
const TARGET_ID = 'course-v1:MITx+parity_check+run2';

const zipjs = require('@zip.js/zip.js');
zipjs.configure({ useWebWorkers: false });
const LTICore = require(path.join(ROOT, 'lti-core.js'));
const LTIStream = require(path.join(ROOT, 'stream-core.js'));

let failures = 0;
function check(label, ok, detail) {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
}

// ---------- JS pipeline ----------
async function runJs(mode) {
  const canvasBlob = new Blob([fs.readFileSync(IMSCC)]);
  const reader = new zipjs.ZipReader(new zipjs.BlobReader(canvasBlob));
  const canvasResult = await LTIStream.readCanvasLinks(reader);

  let mapping;
  if (mode === 'target') {
    mapping = LTICore.blindRewriteMapping(canvasResult.ltiLinks, TARGET_ID);
  } else {
    const olx = await LTIStream.scanOlxStream(new Blob([fs.readFileSync(TARGZ)]).stream());
    if (olx.errors.length) throw new Error('JS OLX errors: ' + olx.errors.join('; '));
    mapping = LTICore.mapLinks(canvasResult.ltiLinks, olx);
  }

  const writer = new zipjs.ZipWriter(new zipjs.BlobWriter('application/zip'));
  await LTIStream.streamRewriteCanvas(reader, writer, mapping);
  const blob = await writer.close();
  await reader.close();

  return {
    imscc: Buffer.from(await blob.arrayBuffer()),
    csv: Buffer.from(LTICore.generateAuditCsv(mapping), 'utf-8'),
    mapping,
  };
}

// ---------- Python pipeline ----------
function runPython(mode) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'parity-py-'));
  const inDir = fs.mkdtempSync(path.join(os.tmpdir(), 'parity-in-'));
  // The CLI scans a folder; give it its own copy so stray files in tests/
  // (previous outputs, notes) can't affect the run.
  fs.copyFileSync(IMSCC, path.join(inDir, path.basename(IMSCC)));
  const args = [path.join(ROOT, 'cli.py'), inDir, '-o', outDir];
  if (mode === 'target') {
    args.push('-t', TARGET_ID);
  } else {
    fs.copyFileSync(TARGZ, path.join(inDir, path.basename(TARGZ)));
  }
  execFileSync('python3', args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'inherit'] });

  const imsccOut = fs.readdirSync(outDir).find((f) => f.endsWith('_updated.imscc'));
  return {
    imscc: fs.readFileSync(path.join(outDir, imsccOut)),
    csv: fs.readFileSync(path.join(outDir, 'lti_audit_report.csv')),
  };
}

// ---------- comparison ----------
async function zipEntries(buf) {
  const reader = new zipjs.ZipReader(new zipjs.BlobReader(new Blob([buf])));
  const files = new Map(); // name -> Buffer
  const dirs = new Set();
  for (const e of await reader.getEntries()) {
    if (e.directory) dirs.add(e.filename.replace(/\/$/, ''));
    else files.set(e.filename, Buffer.from(await e.getData(new zipjs.Uint8ArrayWriter())));
  }
  await reader.close();
  // Only EMPTY directories are significant: Python writes files + empty dirs,
  // while zip.js copies every original entry incl. non-empty dir entries.
  // Both unpack to the same tree, so compare files strictly and empty dirs.
  const emptyDirs = new Set(
    [...dirs].filter((d) => ![...files.keys()].some((f) => f.startsWith(d + '/'))));
  return { files, emptyDirs };
}

async function compareMode(mode) {
  console.log(`\nMode: ${mode === 'target' ? 'unverified rewrite (--target)' : 'verified (edX export)'}`);
  const js = await runJs(mode);
  const py = runPython(mode);

  check('audit CSV byte-identical', js.csv.equals(py.csv),
        `JS ${js.csv.length}B vs Python ${py.csv.length}B`);

  const [jsE, pyE] = [await zipEntries(js.imscc), await zipEntries(py.imscc)];
  const jsNames = [...jsE.files.keys()].sort();
  const pyNames = [...pyE.files.keys()].sort();
  const sameNames = JSON.stringify(jsNames) === JSON.stringify(pyNames);
  check(`imscc file sets match (${jsNames.length} files)`, sameNames,
        'only in JS: ' + jsNames.filter((n) => !pyE.files.has(n)).slice(0, 5).join(', ') +
        ' | only in Python: ' + pyNames.filter((n) => !jsE.files.has(n)).slice(0, 5).join(', '));

  if (sameNames) {
    const diff = jsNames.filter((n) => !jsE.files.get(n).equals(pyE.files.get(n)));
    check('imscc file contents identical', diff.length === 0,
          'differs: ' + diff.slice(0, 5).join(', '));
  }
  check('empty directories preserved identically',
        JSON.stringify([...jsE.emptyDirs].sort()) === JSON.stringify([...pyE.emptyDirs].sort()),
        `JS: ${[...jsE.emptyDirs].join(', ') || '(none)'} | Python: ${[...pyE.emptyDirs].join(', ') || '(none)'}`);
  return js.mapping;
}

(async () => {
  console.log('Python ↔ JS parity check');

  const verified = await compareMode('verified');
  check('verified mode found links', verified.total > 0);
  check('verified mode matched links', verified.matched > 0);

  const target = await compareMode('target');
  check('target mode marked UNVERIFIED', target.warnings.some((w) => w.includes('UNVERIFIED')));
  check('target mode rewrote to target ID',
        target.mappedLinks.some((m) => m.status === 'MATCHED' && m.newUrl.includes('parity_check+run2')));

  console.log(failures ? `\n${failures} check(s) FAILED` : '\nAll checks passed.');
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
