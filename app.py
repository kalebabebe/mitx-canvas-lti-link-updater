"""
Flask Web Application for Canvas LTI Link Updater

Accepts a Canvas IMSCC export and an edX OLX export, maps LTI links
from the old Canvas course to the new edX course, and produces an
updated IMSCC file with corrected LTI URLs plus an audit CSV report.
"""

import gc
import logging
import os
import shutil
import traceback
from pathlib import Path

from flask import Flask, render_template, request, send_file, jsonify, session
from werkzeug.utils import secure_filename

from src.file_detect import identify
from src.parsers.canvas_lti_parser import CanvasLTIParser
from src.parsers.olx_parser import OLXParser
from src.processors.lti_mapper import LTIMapper
from src.generators.imscc_updater import IMSCCUpdater
from src.generators.audit_csv import generate_audit_csv

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB per file
app.config['UPLOAD_FOLDER'] = '/tmp/lti_uploads'
app.config['OUTPUT_FOLDER'] = '/tmp/lti_outputs'

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def cleanup_tmp_folders():
    """
    Clear old files from upload/output directories to prevent disk
    exhaustion on constrained hosts (Render, PythonAnywhere, etc.).
    """
    for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
        try:
            if os.path.exists(folder):
                shutil.rmtree(folder)
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            logger.warning(f'Could not clean {folder}: {e}')


def resolve_uploads():
    """
    Save uploaded files and identify which is the Canvas export and which
    is the edX export — by CONTENT, not by file name or field name.

    Accepts either:
      - 'files': two files in any order (new single-drop-zone UI), or
      - 'canvas_file' + 'edx_file': legacy named fields.

    Returns (info, error_response) where info is
    (canvas_path, edx_path, canvas_filename). Exactly one is None.
    """
    cleanup_tmp_folders()

    uploads = request.files.getlist('files')
    if not uploads:
        uploads = []
        for field in ('canvas_file', 'edx_file'):
            if field in request.files and request.files[field].filename:
                uploads.append(request.files[field])

    uploads = [f for f in uploads if f and f.filename]
    if len(uploads) != 2:
        return None, (jsonify({
            'error': 'Please upload exactly two files: a Canvas export and an edX export.',
            'details': [f'Received {len(uploads)} file(s).'],
        }), 400)

    saved = []
    for i, f in enumerate(uploads):
        name = secure_filename(f.filename) or f'upload_{i}'
        path = os.path.join(app.config['UPLOAD_FOLDER'], f'{i}_{name}')
        f.save(path)
        saved.append((f.filename, path))

    kinds = {}
    for original_name, path in saved:
        kind = identify(path)
        kinds.setdefault(kind, []).append((original_name, path))

    if 'canvas' not in kinds or 'edx' not in kinds:
        details = []
        for kind, label in [('canvas', 'Canvas export (a ZIP containing imsmanifest.xml)'),
                            ('edx', 'edX export (a .tar.gz containing course.xml)')]:
            names = [n for n, _ in kinds.get(kind, [])]
            details.append(f'{label}: {", ".join(names) if names else "NOT FOUND"}')
        for n, _ in kinds.get('unknown', []):
            details.append(f'Unrecognized file: {n}')
        return None, (jsonify({
            'error': 'Could not identify one Canvas export and one edX export '
                     'among the uploaded files.',
            'details': details,
        }), 400)

    canvas_name, canvas_path = kinds['canvas'][0]
    edx_name, edx_path = kinds['edx'][0]
    logger.info(f'Identified uploads: canvas={canvas_name}, edx={edx_name}')
    return (canvas_path, edx_path, secure_filename(canvas_name) or 'course.imscc'), None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    """Render the upload page."""
    max_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return render_template('index.html', max_file_size_mb=max_mb)


@app.route('/preview', methods=['POST'])
def preview():
    """
    Parse both uploads and report what WOULD happen, without generating
    anything. The UI shows this as a confirmation step so wrong-file
    mistakes are caught before processing.
    """
    canvas_parser = None
    olx_parser = None

    try:
        info, err = resolve_uploads()
        if err:
            return err

        canvas_path, edx_path, canvas_filename = info

        canvas_parser = CanvasLTIParser(canvas_path)
        canvas_result = canvas_parser.parse()
        if canvas_result.errors:
            return jsonify({'error': 'Failed to parse Canvas export.',
                            'details': canvas_result.errors}), 400
        if not canvas_result.lti_links:
            return jsonify({
                'error': 'No LTI links found in the Canvas export.',
                'details': ['Make sure you exported the Canvas course that '
                            'contains the edX LTI links (usually last semester\'s course).'],
            }), 400

        olx_parser = OLXParser(edx_path)
        olx_result = olx_parser.parse()
        if olx_result.errors:
            return jsonify({'error': 'Failed to parse edX export.',
                            'details': olx_result.errors}), 400
        if not olx_result.blocks:
            return jsonify({'error': 'No blocks found in the edX export.',
                            'details': ['The OLX export appears to be empty.']}), 400

        # Dry-run the mapping for accurate counts and pairing warnings
        mapping = LTIMapper(canvas_result.lti_links, olx_result).map()

        return jsonify({
            'success': True,
            'preview': {
                'canvas_course_title': canvas_result.course_title,
                'canvas_filename': canvas_filename,
                'lti_link_count': len(canvas_result.lti_links),
                'old_course_id': mapping.old_course_id,
                'new_course_id': mapping.new_course_id,
                'edx_course_title': olx_result.course_title,
                'will_update': mapping.matched_count,
                'will_skip': mapping.missing_count,
                'warnings': mapping.warnings,
            },
        })

    except Exception as e:
        logger.error(f'Preview failed: {traceback.format_exc()}')
        return jsonify({'error': 'Preview failed.', 'details': [str(e)]}), 500

    finally:
        for p in (canvas_parser, olx_parser):
            if p:
                try:
                    p.cleanup()
                except Exception:
                    pass


@app.route('/process', methods=['POST'])
def process():
    """
    Handle file uploads and run the full LTI link mapping pipeline.

    Accepts two files in any order via the 'files' field (identified by
    content), or legacy 'canvas_file'/'edx_file' named fields.
    """
    canvas_parser = None
    olx_parser = None
    step = 'initializing'

    try:
        # ------------------------------------------------------------------
        # Validate, save, and identify uploads (by content)
        # ------------------------------------------------------------------
        step = 'identifying uploaded files'
        info, err = resolve_uploads()
        if err:
            return err

        canvas_path, edx_path, canvas_filename = info

        # ------------------------------------------------------------------
        # Phase 1: Parse Canvas IMSCC for LTI links
        # ------------------------------------------------------------------
        step = 'parsing Canvas export'
        canvas_parser = CanvasLTIParser(canvas_path)
        canvas_result = canvas_parser.parse()

        if canvas_result.errors:
            return jsonify({
                'error': 'Failed to parse Canvas export.',
                'details': canvas_result.errors,
            }), 400

        if not canvas_result.lti_links:
            return jsonify({
                'error': 'No LTI links found in the Canvas export.',
                'details': [
                    'The uploaded .imscc file does not contain any LTI External Tool links.',
                    'Make sure you exported the correct Canvas course that contains edX LTI links.',
                ],
            }), 400

        logger.info(f'Phase 1 complete: {len(canvas_result.lti_links)} LTI links found')

        # ------------------------------------------------------------------
        # Phase 2: Parse edX OLX export
        # ------------------------------------------------------------------
        step = 'parsing edX export'
        olx_parser = OLXParser(edx_path)
        olx_result = olx_parser.parse()

        if olx_result.errors:
            return jsonify({
                'error': 'Failed to parse edX export.',
                'details': olx_result.errors + [
                    'Expected a .tar.gz file exported from edX Studio '
                    '(Course > Export > Download).'
                ],
            }), 400

        if not olx_result.blocks:
            return jsonify({
                'error': 'No blocks found in the edX export.',
                'details': [
                    'The OLX export appears to be empty or could not be parsed.',
                    'Please verify the export was created correctly in edX Studio.',
                ],
            }), 400

        logger.info(f'Phase 2 complete: {len(olx_result.blocks)} blocks in {olx_result.course_id}')

        # ------------------------------------------------------------------
        # Phase 3: Validate and map
        # ------------------------------------------------------------------
        step = 'mapping LTI links'
        mapper = LTIMapper(canvas_result.lti_links, olx_result)
        mapping_result = mapper.map()

        logger.info(
            f'Phase 3 complete: {mapping_result.matched_count} matched, '
            f'{mapping_result.missing_count} missing, '
            f'{mapping_result.new_only_count} new-only'
        )

        # ------------------------------------------------------------------
        # Phase 4: Generate updated IMSCC
        # ------------------------------------------------------------------
        step = 'generating updated Canvas export'
        updater = IMSCCUpdater(
            canvas_parser.get_extract_dir(),
            mapping_result,
            original_filename=canvas_filename,
        )
        updated_imscc_path = updater.generate(app.config['OUTPUT_FOLDER'])
        updated_imscc_name = os.path.basename(updated_imscc_path)

        logger.info(f'Phase 4 complete: {updated_imscc_name}')

        # ------------------------------------------------------------------
        # Phase 5: Generate audit CSV
        # ------------------------------------------------------------------
        step = 'generating audit report'
        audit_csv_path = generate_audit_csv(
            mapping_result,
            app.config['OUTPUT_FOLDER'],
            filename='lti_audit_report.csv',
        )

        logger.info('Phase 5 complete: audit CSV generated')

        # ------------------------------------------------------------------
        # Clean up uploads and temp dirs
        # ------------------------------------------------------------------
        step = 'cleaning up'
        for path in [canvas_path, edx_path]:
            if os.path.exists(path):
                os.remove(path)

        if canvas_parser:
            canvas_parser.cleanup()
        if olx_parser:
            olx_parser.cleanup()
        gc.collect()

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        # Prepare results for the frontend
        results_data = []
        for link in mapping_result.mapped_links:
            results_data.append({
                'status': link.status,
                'resource_title': link.resource_title,
                'old_lti_url': link.old_lti_url,
                'new_lti_url': link.new_lti_url,
                'block_id': link.block_id,
                'block_type': link.block_type,
                'edx_location': link.edx_location,
                'notes': link.notes,
            })

        # Sort: MISSING first, then NEW_ONLY, then MATCHED
        status_order = {'MISSING': 0, 'NEW_ONLY': 1, 'MATCHED': 2}
        results_data.sort(key=lambda x: (
            status_order.get(x['status'], 99),
            x['resource_title'].lower()
        ))

        return jsonify({
            'success': True,
            'summary': {
                'total_lti_links': mapping_result.total_lti_links,
                'matched': mapping_result.matched_count,
                'missing': mapping_result.missing_count,
                'new_only': mapping_result.new_only_count,
                'old_course_id': mapping_result.old_course_id,
                'new_course_id': mapping_result.new_course_id,
                'canvas_course_title': canvas_result.course_title,
                'edx_course_title': olx_result.course_title,
            },
            'warnings': mapping_result.warnings,
            'results': results_data,
            'downloads': {
                'updated_imscc': f'/download/{updated_imscc_name}',
                'audit_csv': '/download/lti_audit_report.csv',
            },
        })

    except Exception as e:
        logger.error(f'Processing failed at step "{step}": {traceback.format_exc()}')

        # Clean up on error
        if canvas_parser:
            try:
                canvas_parser.cleanup()
            except Exception:
                pass
        if olx_parser:
            try:
                olx_parser.cleanup()
            except Exception:
                pass

        return jsonify({
            'error': f'Processing failed during: {step}',
            'details': [str(e)],
        }), 500


@app.route('/download/<path:filename>')
def download(filename):
    """Serve a generated file for download.

    Uses secure_filename and confirms the resolved path stays inside the
    output folder to prevent path-traversal (e.g. /download/../../etc/passwd).
    """
    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid file name.'}), 400

    output_root = os.path.realpath(app.config['OUTPUT_FOLDER'])
    file_path = os.path.realpath(os.path.join(output_root, safe_name))

    # Ensure the resolved path is within the output folder
    if os.path.commonpath([output_root, file_path]) != output_root:
        return jsonify({'error': 'Invalid file path.'}), 400

    if not os.path.isfile(file_path):
        return jsonify({'error': 'File not found. It may have expired — please process again.'}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=safe_name,
    )


@app.route('/health')
def health():
    """Health check endpoint for deployment monitoring."""
    return jsonify({'status': 'healthy'})


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(413)
def file_too_large(e):
    max_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({
        'error': f'File too large. Maximum size is {max_mb}MB per file.',
    }), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Page not found.'}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'An internal server error occurred.'}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
