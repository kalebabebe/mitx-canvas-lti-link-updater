#!/usr/bin/env python3
"""
Local CLI for the Canvas LTI Link Updater.

Drop a Canvas course export (.imscc) and an edX OLX export (.tar.gz) into a
folder — file names don't matter — then run:

    python cli.py [folder]

The tool detects each file by content (a ZIP containing imsmanifest.xml is
the Canvas export; a gzipped tar containing course.xml is the edX export),
maps the LTI links, and writes into the same folder:

    <canvas-export-name>_updated.imscc
    lti_audit_report.csv
"""

import argparse
import re
import sys
from pathlib import Path

from src.file_detect import is_canvas_export, is_edx_export
from src.parsers.canvas_lti_parser import CanvasLTIParser
from src.parsers.olx_parser import OLXParser
from src.processors.lti_mapper import LTIMapper, MappedLink, MappingResult
from src.generators.imscc_updater import IMSCCUpdater
from src.generators.audit_csv import generate_audit_csv

COURSE_ID_RE = re.compile(r'^course-v1:([^+]+)\+([^+]+)\+([^+]+)$')


def find_inputs(folder: Path):
    """Scan a folder and identify the Canvas and edX exports by content."""
    canvas, edx = [], []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith('.'):
            continue
        if f.name.endswith('_updated.imscc'):
            continue  # previous output of this tool
        if is_canvas_export(f):
            canvas.append(f)
        elif is_edx_export(f):
            edx.append(f)
    return canvas, edx


def blind_rewrite_mapping(lti_links, target_course_id: str) -> MappingResult:
    """
    Build a MappingResult that rewrites every LTI link's course identifiers
    to the target course WITHOUT verifying blocks against an edX export.
    Use only when the new course is an exact copy (same block IDs).
    """
    m = COURSE_ID_RE.match(target_course_id)
    if not m:
        sys.exit(f'Error: --target must look like course-v1:ORG+COURSE+RUN '
                 f'(got: {target_course_id})')
    new_org, new_course, new_run = m.groups()

    result = MappingResult()
    result.total_lti_links = len(lti_links)
    result.new_course_id = target_course_id
    result.warnings.append(
        'UNVERIFIED REWRITE: block IDs were not checked against an edX export. '
        'Confirm the new course is an exact copy of the old one.'
    )

    for link in lti_links:
        if link.raw_course_id:
            if not result.old_course_id:
                result.old_course_id = link.raw_course_id
            old_triple = link.raw_course_id[len('course-v1:'):]
            new_triple = f'{new_org}+{new_course}+{new_run}'
            new_url = link.launch_url.replace(old_triple, new_triple)
            result.mapped_links.append(MappedLink(
                status='MATCHED',
                resource_title=link.title,
                old_lti_url=link.launch_url,
                new_lti_url=new_url,
                block_id=link.edx_block_id,
                block_type=link.edx_block_type,
                notes='Rewritten without verification (--target mode)',
                resource_id=link.resource_id,
                xml_file=link.xml_file,
            ))
            result.matched_count += 1
        else:
            result.mapped_links.append(MappedLink(
                status='MISSING',
                resource_title=link.title,
                old_lti_url=link.launch_url,
                notes='No course ID in URL; left unchanged',
                resource_id=link.resource_id,
                xml_file=link.xml_file,
            ))
            result.missing_count += 1

    return result


def main():
    ap = argparse.ArgumentParser(description='Update Canvas LTI links from an edX export.')
    ap.add_argument('folder', nargs='?', default='.',
                    help='Folder containing the Canvas and edX exports (default: current dir)')
    ap.add_argument('-o', '--output', default=None,
                    help='Output folder (default: same as input folder)')
    ap.add_argument('-t', '--target', default=None, metavar='COURSE_ID',
                    help='Rewrite all LTI links to this course-v1:ORG+COURSE+RUN '
                         'WITHOUT verifying against an edX export. Use only when '
                         'the new course is an exact copy.')
    args = ap.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f'Error: {folder} is not a directory')
    out_dir = Path(args.output).resolve() if args.output else folder

    canvas_files, edx_files = find_inputs(folder)

    if len(canvas_files) != 1 or (args.target is None and len(edx_files) != 1):
        print(f'Scanned: {folder}')
        print(f'  Canvas exports found: {[f.name for f in canvas_files] or "none"}')
        print(f'  edX exports found:    {[f.name for f in edx_files] or "none"}')
        if args.target:
            sys.exit('Error: need exactly one Canvas export (ZIP with imsmanifest.xml) '
                     'in the folder.')
        sys.exit('Error: need exactly one Canvas export (ZIP with imsmanifest.xml) '
                 'and one edX export (.tar.gz with course.xml) in the folder.')

    canvas_path = canvas_files[0]
    print(f'Canvas export: {canvas_path.name}')

    canvas_parser = CanvasLTIParser(str(canvas_path))
    olx_parser = None
    try:
        canvas_result = canvas_parser.parse()
        if canvas_result.errors:
            sys.exit('Error parsing Canvas export:\n  ' + '\n  '.join(canvas_result.errors))
        if not canvas_result.lti_links:
            sys.exit('No LTI links found in the Canvas export.')
        print(f'Found {len(canvas_result.lti_links)} LTI link(s) in "{canvas_result.course_title}"')

        if args.target:
            print(f'Target course (unverified rewrite): {args.target}')
            mapping = blind_rewrite_mapping(canvas_result.lti_links, args.target)
        else:
            edx_path = edx_files[0]
            print(f'edX export:    {edx_path.name}')
            olx_parser = OLXParser(str(edx_path))
            olx_result = olx_parser.parse()
            if olx_result.errors:
                sys.exit('Error parsing edX export:\n  ' + '\n  '.join(olx_result.errors))
            if not olx_result.blocks:
                sys.exit('No blocks found in the edX export.')
            print(f'Found {len(olx_result.blocks)} block(s) in {olx_result.course_id}')
            mapping = LTIMapper(canvas_result.lti_links, olx_result).map()
        print(f'Mapping: {mapping.matched_count} matched, '
              f'{mapping.missing_count} missing, {mapping.new_only_count} new-only')
        for w in mapping.warnings:
            print(f'  Warning: {w}')

        updater = IMSCCUpdater(canvas_parser.get_extract_dir(), mapping,
                               original_filename=canvas_path.name)
        imscc_out = updater.generate(str(out_dir))
        csv_out = generate_audit_csv(mapping, str(out_dir))

        print(f'\nWrote: {imscc_out}')
        print(f'Wrote: {csv_out}')
        if mapping.missing_count:
            print(f'\nReview the {mapping.missing_count} MISSING link(s) at the top of the audit CSV.')
    finally:
        canvas_parser.cleanup()
        if olx_parser:
            olx_parser.cleanup()


if __name__ == '__main__':
    main()
