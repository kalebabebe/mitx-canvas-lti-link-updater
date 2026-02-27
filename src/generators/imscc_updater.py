"""
IMSCC Updater

Takes the original Canvas IMSCC extract directory and the mapping results,
then produces an updated .imscc file with all MATCHED LTI URLs replaced
with their new course URLs. All non-LTI content is preserved exactly as-is.
"""

import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, Optional

from src.processors.lti_mapper import MappedLink, MappingResult

logger = logging.getLogger(__name__)

# XML namespaces (same as canvas_lti_parser)
NAMESPACES = {
    'blti': 'http://www.imsglobal.org/xsd/imsbasiclti_v1p0',
    'lticm': 'http://www.imsglobal.org/xsd/imslticm_v1p0',
    'lticp': 'http://www.imsglobal.org/xsd/imslticp_v1p0',
    'canvas': 'http://canvas.instructure.com/xsd/cccv1p0',
}

for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


class IMSCCUpdater:
    """
    Updates LTI URLs in an extracted IMSCC directory and repackages it.

    Usage:
        updater = IMSCCUpdater(extract_dir, mapping_result)
        output_path = updater.generate(output_dir)
    """

    def __init__(self, extract_dir: Path, mapping_result: MappingResult,
                 original_filename: str = 'updated_course.imscc'):
        self.extract_dir = extract_dir
        self.mapping = mapping_result
        self.original_filename = original_filename
        self._update_count = 0

    def generate(self, output_dir: str) -> str:
        """
        Generate the updated .imscc file.

        Args:
            output_dir: Directory where the output file should be placed.

        Returns:
            Path to the generated .imscc file.
        """
        os.makedirs(output_dir, exist_ok=True)

        # Step 1: Create a working copy of the extracted IMSCC
        work_dir = tempfile.mkdtemp(prefix='imscc_update_')
        working_copy = Path(work_dir) / 'imscc'

        try:
            shutil.copytree(self.extract_dir, working_copy)

            # Step 2: Build a map of resource_id/xml_file -> new URL
            url_map = self._build_url_map()

            # Step 3: Update LTI XML files
            self._update_lti_files(working_copy, url_map)

            # Step 4: Also update any URL references in module_meta.xml
            self._update_module_meta(working_copy, url_map)

            # Step 5: Repackage as .imscc (ZIP)
            output_filename = self._make_output_filename()
            output_path = os.path.join(output_dir, output_filename)

            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(working_copy):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, working_copy)
                        zf.write(file_path, arcname)

            logger.info(f'Generated updated IMSCC with {self._update_count} URL updates: {output_path}')
            return output_path

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _build_url_map(self) -> Dict[str, Dict]:
        """
        Build a mapping from XML file paths and old URLs to new URLs.
        Only includes MATCHED links.
        """
        url_map = {}

        for link in self.mapping.mapped_links:
            if link.status != 'MATCHED' or not link.new_lti_url:
                continue

            # Map by XML file if available
            if link.xml_file:
                url_map[link.xml_file] = {
                    'old_url': link.old_lti_url,
                    'new_url': link.new_lti_url,
                    'resource_id': link.resource_id,
                }

            # Also map by old_url directly for broader matching
            url_map[link.old_lti_url] = {
                'old_url': link.old_lti_url,
                'new_url': link.new_lti_url,
                'resource_id': link.resource_id,
            }

        return url_map

    def _update_lti_files(self, working_copy: Path, url_map: Dict):
        """Update launch URLs in basiclti_link XML files."""
        blti_ns = NAMESPACES['blti']

        for xml_file, mapping_info in url_map.items():
            # Skip entries keyed by URL (not file path)
            if xml_file.startswith('http'):
                continue

            xml_path = working_copy / xml_file
            if not xml_path.exists():
                logger.warning(f'XML file not found for update: {xml_file}')
                continue

            try:
                # Read raw content for string replacement (preserves formatting)
                content = xml_path.read_text(encoding='utf-8')
                old_url = mapping_info['old_url']
                new_url = mapping_info['new_url']

                if old_url in content:
                    content = content.replace(old_url, new_url)
                    xml_path.write_text(content, encoding='utf-8')
                    self._update_count += 1
                    logger.debug(f'Updated URL in {xml_file}')
                else:
                    # Try XML-aware update as fallback
                    self._update_xml_launch_url(xml_path, old_url, new_url)

            except Exception as e:
                logger.warning(f'Failed to update {xml_file}: {e}')

        # Also do a broader sweep: update any remaining references
        # using the old/new course ID pattern
        if self.mapping.old_course_id and self.mapping.new_course_id:
            self._sweep_update_course_ids(working_copy)

    def _update_xml_launch_url(self, xml_path: Path, old_url: str, new_url: str):
        """Update launch URL using XML parsing as a fallback."""
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            blti_ns = NAMESPACES['blti']
            updated = False

            for tag_name in ['launch_url', 'secure_launch_url']:
                for el in root.iter():
                    local_tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                    if local_tag == tag_name and el.text:
                        if el.text.strip() == old_url:
                            el.text = new_url
                            updated = True

            if updated:
                tree.write(xml_path, xml_declaration=True, encoding='UTF-8')
                self._update_count += 1

        except ET.ParseError as e:
            logger.warning(f'XML parse error in {xml_path}: {e}')

    def _update_module_meta(self, working_copy: Path, url_map: Dict):
        """Update any LTI URLs referenced in module_meta.xml."""
        meta_path = working_copy / 'course_settings' / 'module_meta.xml'
        if not meta_path.exists():
            return

        try:
            content = meta_path.read_text(encoding='utf-8')
            modified = False

            for key, info in url_map.items():
                old_url = info['old_url']
                new_url = info['new_url']
                if old_url in content:
                    content = content.replace(old_url, new_url)
                    modified = True

            if modified:
                meta_path.write_text(content, encoding='utf-8')
                logger.debug('Updated URLs in module_meta.xml')

        except Exception as e:
            logger.warning(f'Failed to update module_meta.xml: {e}')

    def _sweep_update_course_ids(self, working_copy: Path):
        """
        Sweep through all XML files to update any remaining references
        to the old course ID with the new course ID.
        """
        old_id = self.mapping.old_course_id
        new_id = self.mapping.new_course_id

        if not old_id or not new_id or old_id == new_id:
            return

        # Also build old/new block-v1 prefixes
        old_block_prefix = old_id.replace('course-v1:', 'block-v1:')
        new_block_prefix = new_id.replace('course-v1:', 'block-v1:')

        for xml_path in working_copy.rglob('*.xml'):
            try:
                content = xml_path.read_text(encoding='utf-8')
                if old_id in content or old_block_prefix in content:
                    content = content.replace(old_id, new_id)
                    content = content.replace(old_block_prefix, new_block_prefix)
                    xml_path.write_text(content, encoding='utf-8')
            except Exception:
                pass  # Skip binary or unreadable files

    def _make_output_filename(self) -> str:
        """Generate a descriptive output filename."""
        base = Path(self.original_filename).stem
        # Remove any existing _updated suffix
        base = re.sub(r'_updated$', '', base)
        return f'{base}_updated.imscc'
