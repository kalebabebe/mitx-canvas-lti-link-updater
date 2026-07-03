# MITx Canvas LTI Link Updater

**Live tool: https://kalebabebe.github.io/mitx-canvas-lti-link-updater/**

Updates LTI links in a Canvas course export so they point to a new edX course run. When an edX course is re-run or copied for a new semester, the block IDs stay the same but the course identifier changes — leaving every Canvas LTI link pointing at the old course. This tool rewrites those links and produces a ready-to-import Canvas export plus an audit report.

Year-over-year workflow it supports:

1. edX course is re-run/copied in Studio (e.g. `ulmo_general` → `ulmo_general_2`).
2. Export the old Canvas course (`.imscc`) and the new edX course (`.tar.gz`).
3. Run this tool (web or CLI).
4. Import the `_updated.imscc` into the new semester's Canvas course.

## Two ways to run it

Two front ends over one shared pipeline:

| | Best for | Course size | Hosting |
|---|---|---|---|
| **A. Browser app** (repo root) | Most users — nothing to install | Any, incl. 600MB+ (streams) | Free, GitHub Pages |
| **B. CLI** (`cli.py`) | Power users comfortable with a terminal; offline; batch | Any | None (runs locally) |

### A. Browser app (GitHub Pages) — recommended for most users

Static site at the repository root, served at **https://kalebabebe.github.io/mitx-canvas-lti-link-updater/**. Runs entirely client-side: files never leave the user's computer, no upload, no server. All assets are local (zip.js is vendored in `vendor/`), so there is no CDN dependency.

Uses a **streaming** pipeline (zip.js + native gzip streams) that keeps memory flat regardless of course size, so 600MB+ courses work where the naive in-browser approach would exhaust tab memory. In Chrome/Edge it streams the rebuilt `.imscc` straight to disk via the File System Access API; other browsers fall back to an in-memory download. Browsers without `DecompressionStream` (e.g. Safari < 16.4) get a clear unsupported-browser message on load.

Hosting: repo Settings → Pages → Deploy from a branch → `main` branch, `/ (root)` folder. To try locally, serve the repo (`python -m http.server`) and open the printed URL — opening `index.html` via `file://` won't work because of browser security rules.

**Advanced (unverified) mode:** if the user doesn't have the edX export but the new course is an exact copy, the upload screen has an "Advanced" option to rewrite all links to a target course ID without verification — same semantics as the CLI's `--target` (below).

### B. Local CLI (folder-based)

Drop the two export files into any folder — **file names don't matter**, the CLI detects each file by content (a ZIP containing `imsmanifest.xml` is the Canvas export; a gzipped tar containing `course.xml` is the edX export):

```
python cli.py /path/to/folder
```

Pure standard library — no `pip install` needed (Python 3.9+). Outputs are written to the same folder (or elsewhere with `-o`):

- `<canvas-export-name>_updated.imscc` — import this into Canvas
- `lti_audit_report.csv` — every link's status, sorted so problems are on top

### Unverified rewrite (`--target`)

If you don't have an edX export but are **confident the new course is an exact copy** (same block IDs — true for Studio re-runs and export/import copies), you can rewrite all LTI links to a target course ID without verification:

```
python cli.py /path/to/folder -t course-v1:MITx+ulmo_general_2+smoketest
```

Only a Canvas export needs to be in the folder. The audit CSV is marked `UNVERIFIED REWRITE`. Prefer the verified mode when possible — it's the only way to catch links to blocks that don't exist in the new course. After an unverified rewrite, spot-check a link or two after importing into Canvas.

## Audit report statuses

| Status | Meaning | Action |
|---|---|---|
| `MATCHED` | Block found in new course (or rewritten in `--target` mode); URL updated | None |
| `MISSING` | Canvas link references a block not found in the new course; URL left unchanged | Fix or remove the link manually |
| `NEW_ONLY` | A linkable block (sequential/vertical) exists in the new course with no Canvas link | Informational |

Links with no `course-v1:`/`block-v1:` identifier (e.g. the bare LTI tool configuration URL) are never rewritten.

## Repo layout

```
index.html                        A. Browser app UI (GitHub Pages serves the repo root)
style.css                           Styling
lti-core.js                         Shared pure logic (parse/map/CSV), JS port
stream-core.js                      Streaming readers/writers (zip.js + gzip)
vendor/zip-full.min.js              Vendored zip.js (pinned, no CDN)

cli.py                            B. Local folder-based CLI
src/file_detect.py                Content-based file identification
src/parsers/canvas_lti_parser.py  Finds LTI links in the .imscc (Python)
src/parsers/olx_parser.py         Builds block inventory from the .tar.gz (Python)
src/processors/lti_mapper.py      Matches links to blocks, builds new URLs (Python)
src/generators/imscc_updater.py   Rewrites URLs, repackages the .imscc (Python)
src/generators/audit_csv.py       Audit CSV (Python)

docs/user-guide.html              In-app user guide (linked from the browser app)
docs/internal-docs.html           Internal docs: architecture, hosting, failure modes

tests/                            Sample export fixtures + parity check
tests/parity_check.js             Asserts Python and JS pipelines produce identical output
.github/workflows/parity.yml      Runs the parity check on every push/PR
```

## Testing

The Python pipeline (`src/`, used by `cli.py`) and the JS pipeline (`lti-core.js` + `stream-core.js`, used by the browser app) implement the same logic. **Any change to matching/rewriting logic must be made in both** — including warning text, sort order, and CSV formatting. The parity check verifies they produce identical output (byte-identical CSVs; identical entry contents in the `.imscc`) on the test fixtures, in both verified and `--target` modes:

```
npm install
npm test
```

Requires Node 18+ and Python 3.9+. CI runs this automatically on every push and pull request.

## Notes for maintainers

- The OLX block inventory includes chapters, sequentials, and verticals — MITx LTI links most commonly target sequentials and verticals, not leaf components.
- macOS metadata files (`._*`, `__MACOSX`) inside the tar.gz are ignored.
- The updated `.imscc` is identical to the input except for the rewritten LTI URLs; empty directories are preserved.
- **Memory:** the browser app streams. Measured peak RSS ~330MB for ~530MB of input (flat regardless of media size), vs ~930MB for 288MB input with a non-streaming load — which is why the app uses a streaming pipeline.
- Documentation is split by audience: `mitx-canvas-lti-link-updater-user-guide.html` (course teams, plain language — published on Zendesk; kept at repo root temporarily) and `docs/internal-docs.html` (ETs/support: CLI, architecture, hosting, failure modes). This README and the internal doc are internal-facing; don't link them from user-facing articles.

## Contact

mitx-support@mit.edu
