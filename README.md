# MITx Canvas LTI Link Updater

Updates LTI links in a Canvas course export so they point to a new edX course run. When an edX course is re-run or copied for a new semester, the block IDs stay the same but the course identifier changes — leaving every Canvas LTI link pointing at the old course. This tool rewrites those links and produces a ready-to-import Canvas export plus an audit report.

Year-over-year workflow it supports:

1. edX course is re-run/copied in Studio (e.g. `ulmo_general` → `ulmo_general_2`).
2. Export the old Canvas course (`.imscc`) and the new edX course (`.tar.gz`).
3. Run this tool (web or CLI).
4. Import the `_updated.imscc` into the new semester's Canvas course.

## Three ways to run it

There are three front ends over one shared pipeline. Pick by audience and course size:

| | Best for | Course size | Hosting |
|---|---|---|---|
| **A. Browser app** (`docs/`) | Most users — nothing to install | Any, incl. 600MB+ (streams) | Free, GitHub Pages |
| **B. CLI** (`cli.py`) | Power users comfortable with a terminal; offline; batch | Any | None (runs locally) |
| **C. Hosted Flask** (`app.py`) | Optional fallback; central URL | Limited by host upload/disk | Paid tier recommended |

The browser app and CLI are the primary paths; the hosted Flask app is kept as an option.

### A. Browser app (GitHub Pages) — recommended for most users

Static site in `docs/`. Runs entirely client-side: files never leave the user's computer, no upload, no server. Uses a **streaming** pipeline (zip.js + native gzip streams) that keeps memory flat regardless of course size, so 600MB+ courses work where the naive in-browser approach would exhaust tab memory. In Chrome/Edge it streams the rebuilt `.imscc` straight to disk via the File System Access API; other browsers fall back to an in-memory download.

To host: repo Settings → Pages → Source = `main` branch, `/docs` folder. To try locally, serve the folder (`python -m http.server` from `docs/`) and open it — opening `index.html` via `file://` won't work because of module/CORS rules.

### C. Hosted Flask web app

```
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

Upload the `.imscc` and `.tar.gz`, confirm, download the updated export and audit CSV. Deployment configs for Render (`render.yaml`) and PythonAnywhere are included. Note free tiers cap uploads (~70MB practical on PythonAnywhere); large courses need a paid tier or the browser app / CLI.

### B. Local CLI (folder-based)

Drop the two export files into any folder — **file names don't matter**, the CLI detects each file by content (a ZIP containing `imsmanifest.xml` is the Canvas export; a gzipped tar containing `course.xml` is the edX export):

```
python cli.py /path/to/folder
```

Outputs are written to the same folder (or elsewhere with `-o`):

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
docs/                             A. Browser app (GitHub Pages source)
  index.html                        Streaming UI (converter visual style)
  style.css                         Styling
  lti-core.js                       Shared pure logic (parse/map/CSV), JS port
  stream-core.js                    Streaming readers/writers (zip.js + gzip)
  user-guide.html                   In-app user guide

cli.py                            B. Local folder-based CLI
app.py                            C. Flask web app (upload → process → download)
src/file_detect.py                Content-based file identification (shared)
src/parsers/canvas_lti_parser.py  Finds LTI links in the .imscc (Python)
src/parsers/olx_parser.py         Builds block inventory from the .tar.gz (Python)
src/processors/lti_mapper.py      Matches links to blocks, builds new URLs (Python)
src/generators/imscc_updater.py   Rewrites URLs, repackages the .imscc (Python)
src/generators/audit_csv.py       Audit CSV (Python)
templates/index.html              Flask web UI
tests/                            Sample export files
```

The Python pipeline (`src/`, used by `app.py` and `cli.py`) and the JS pipeline (`docs/lti-core.js` + `docs/stream-core.js`) implement the same logic. They are verified to produce **byte-identical** output `.imscc` files against the test fixtures.

## Notes for maintainers

- The OLX block inventory includes chapters, sequentials, and verticals — MITx LTI links most commonly target sequentials and verticals, not leaf components.
- macOS metadata files (`._*`, `__MACOSX`) inside the tar.gz are ignored.
- The updated `.imscc` is byte-identical to the input except for the rewritten LTI URLs; empty directories are preserved.
- `/download/` (Flask) sanitizes filenames and confines paths to the output folder (path-traversal protection).
- **Memory:** the browser app streams. Measured peak RSS ~330MB for ~530MB of input (flat regardless of media size), vs ~930MB for 288MB input with a non-streaming load — which is why `docs/` uses a streaming pipeline.
- **Two implementations:** any change to matching/rewriting logic must be made in both Python (`src/`) and JS (`docs/lti-core.js`, `docs/stream-core.js`) and re-verified for parity.
- Documentation is split by audience: `mitx-canvas-lti-link-updater-user-guide.html` (course teams, plain language — publish this one) and `mitx-canvas-lti-link-updater-internal-docs.html` (ETs/support: CLI, architecture, hosting, deployment, failure modes). This README and the internal doc are internal-facing; don't link them from user-facing articles.

## Contact

mitx-support@mit.edu
