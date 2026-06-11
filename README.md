# MITx Canvas LTI Link Updater

Updates LTI links in a Canvas course export so they point to a new edX course run. When an edX course is re-run or copied for a new semester, the block IDs stay the same but the course identifier changes — leaving every Canvas LTI link pointing at the old course. This tool rewrites those links and produces a ready-to-import Canvas export plus an audit report.

Year-over-year workflow it supports:

1. edX course is re-run/copied in Studio (e.g. `ulmo_general` → `ulmo_general_2`).
2. Export the old Canvas course (`.imscc`) and the new edX course (`.tar.gz`).
3. Run this tool (web or CLI).
4. Import the `_updated.imscc` into the new semester's Canvas course.

## Two ways to run it

### Web app (hosted)

```
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

Upload the `.imscc` and `.tar.gz`, click Process, download the updated export and audit CSV. Deployment configs for Render (`render.yaml`) and PythonAnywhere are included; see the docs HTML for details.

### Local CLI (folder-based)

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
app.py                          Flask web app (upload → process → download)
cli.py                          Local folder-based CLI
src/parsers/canvas_lti_parser.py  Finds LTI links in the .imscc
src/parsers/olx_parser.py         Builds block inventory from the edX .tar.gz
src/processors/lti_mapper.py      Matches links to blocks, builds new URLs
src/generators/imscc_updater.py   Rewrites URLs, repackages the .imscc
src/generators/audit_csv.py       Audit CSV
templates/index.html              Web UI
tests/                            Sample export files
```

## Notes for maintainers

- The OLX block inventory includes chapters, sequentials, and verticals — MITx LTI links most commonly target sequentials and verticals, not leaf components.
- macOS metadata files (`._*`, `__MACOSX`) inside the tar.gz are ignored.
- The updated `.imscc` is byte-identical to the input except for the rewritten LTI URLs; empty directories are preserved.
- `/download/` sanitizes filenames and confines paths to the output folder (path-traversal protection).
- Documentation is split by audience: `mitx-canvas-lti-link-updater-user-guide.html` (course teams, plain language — publish this one) and `mitx-canvas-lti-link-updater-internal-docs.html` (ETs/support: CLI, architecture, hosting, failure modes). This README and the internal doc are internal-facing; don't link them from user-facing articles.

## Contact

mitx-support@mit.edu
