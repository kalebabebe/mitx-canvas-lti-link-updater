"""
Open edX OLX Course Export Parser

Parses .tar.gz OLX (Open Learning XML) exports from edX to build a complete
inventory of all blocks (components) in a course, along with their hierarchy.

OLX directory structure:
    course/
    ├── course.xml          (root: course org, course, run)
    ├── course/             (course-level policies)
    ├── chapter/            (chapters/sections)
    ├── sequential/         (subsections)
    ├── vertical/           (units/verticals)
    ├── problem/            (problem components)
    ├── video/              (video components)
    ├── html/               (HTML components)
    ├── lti_consumer/       (LTI components)
    ├── ...other types...
    └── policies/           (grading, course details)
"""

import logging
import os
import shutil
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class OLXBlock:
    """Represents a single block/component in an edX course."""
    block_id: str               # The url_name / block ID
    block_type: str             # e.g., problem, video, html, lti_consumer
    display_name: str = ''      # Human-readable name
    chapter: str = ''           # Parent chapter display name
    sequential: str = ''        # Parent sequential display name
    vertical: str = ''          # Parent vertical display name
    xml_file: str = ''          # Source XML file path


@dataclass
class OLXParseResult:
    """Results from parsing an OLX export."""
    org: str = ''
    course: str = ''
    run: str = ''
    course_id: str = ''         # Full course-v1:{org}+{course}+{run}
    blocks: Dict[str, OLXBlock] = field(default_factory=dict)  # block_id -> OLXBlock
    block_types: Dict[str, List[str]] = field(default_factory=dict)  # type -> [block_ids]
    errors: List[str] = field(default_factory=list)
    course_title: str = ''


class OLXParser:
    """
    Parses an edX OLX .tar.gz export to inventory all blocks.

    Usage:
        parser = OLXParser(targz_path)
        result = parser.parse()
        parser.cleanup()
    """

    def __init__(self, targz_path: str):
        self.targz_path = Path(targz_path)
        self.extract_dir: Optional[Path] = None
        self.course_root: Optional[Path] = None
        self._temp_dir: Optional[str] = None

    def parse(self) -> OLXParseResult:
        """
        Main entry point. Extracts the tar.gz and parses the OLX structure.

        Returns:
            OLXParseResult with complete block inventory and course metadata.
        """
        result = OLXParseResult()

        # Step 1: Extract tar.gz
        self._temp_dir = tempfile.mkdtemp(prefix='olx_parse_')
        self.extract_dir = Path(self._temp_dir) / 'extracted'
        self.extract_dir.mkdir()

        try:
            with tarfile.open(self.targz_path, 'r:gz') as tf:
                # Security: prevent path traversal
                for member in tf.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        result.errors.append(f'Suspicious path in archive: {member.name}')
                        return result
                tf.extractall(self.extract_dir)
        except tarfile.TarError as e:
            result.errors.append(f'The uploaded file is not a valid .tar.gz archive: {str(e)}')
            return result
        except Exception as e:
            result.errors.append(f'Failed to extract .tar.gz file: {str(e)}')
            return result

        # Step 2: Find course.xml (might be in a subdirectory)
        self.course_root = self._find_course_root()
        if not self.course_root:
            result.errors.append(
                'No course.xml found in the archive. '
                'Please upload a valid edX OLX course export (.tar.gz).'
            )
            return result

        # Step 3: Parse course.xml for org/course/run
        self._parse_course_xml(result)

        # Step 4: Walk the hierarchy to build block inventory
        self._build_block_inventory(result)

        logger.info(
            f'Parsed OLX: {result.org}+{result.course}+{result.run}, '
            f'{len(result.blocks)} blocks found'
        )
        return result

    def _find_course_root(self) -> Optional[Path]:
        """Find the directory containing course.xml."""
        # Direct
        if (self.extract_dir / 'course.xml').exists():
            return self.extract_dir

        # One level deep (common: archive contains a single folder)
        for child in self.extract_dir.iterdir():
            if child.is_dir():
                if (child / 'course.xml').exists():
                    return child
                # Two levels deep
                for grandchild in child.iterdir():
                    if grandchild.is_dir() and (grandchild / 'course.xml').exists():
                        return grandchild

        return None

    def _parse_course_xml(self, result: OLXParseResult):
        """Parse course.xml to extract org, course code, and run."""
        course_xml_path = self.course_root / 'course.xml'

        try:
            tree = ET.parse(course_xml_path)
            root = tree.getroot()

            result.org = root.get('org', '')
            result.course = root.get('course', '')
            result.run = root.get('url_name', '')

            # If url_name points to a file, look there for more details
            if result.run:
                course_detail_path = self.course_root / 'course' / f'{result.run}.xml'
                if course_detail_path.exists():
                    try:
                        detail_tree = ET.parse(course_detail_path)
                        detail_root = detail_tree.getroot()
                        result.course_title = detail_root.get('display_name', '')
                    except ET.ParseError:
                        pass

            if not result.course_title:
                result.course_title = root.get('display_name', '')

            # Build the full course ID
            if result.org and result.course and result.run:
                result.course_id = f'course-v1:{result.org}+{result.course}+{result.run}'

        except ET.ParseError as e:
            result.errors.append(f'Failed to parse course.xml: {str(e)}')

    def _build_block_inventory(self, result: OLXParseResult):
        """
        Walk the OLX directory structure to build a complete inventory
        of all blocks in the course.
        """
        # First, build the hierarchy by walking chapters -> sequentials -> verticals
        chapter_map = self._parse_hierarchy_level('chapter')
        sequential_map = self._parse_hierarchy_level('sequential')
        vertical_map = self._parse_hierarchy_level('vertical')

        # Build parent-child relationships from course -> chapter -> sequential -> vertical
        chapter_children = {}  # chapter_url_name -> [sequential_url_names]
        sequential_children = {}  # sequential_url_name -> [vertical_url_names]
        vertical_children = {}  # vertical_url_name -> [(block_type, block_url_name)]

        for ch_id, ch_data in chapter_map.items():
            chapter_children[ch_id] = ch_data.get('children', [])

        for seq_id, seq_data in sequential_map.items():
            sequential_children[seq_id] = seq_data.get('children', [])

        for vert_id, vert_data in vertical_map.items():
            vertical_children[vert_id] = vert_data.get('children', [])

        # Now walk every component type directory
        component_types = self._find_component_types()

        for block_type in component_types:
            type_dir = self.course_root / block_type
            if not type_dir.is_dir():
                continue

            for xml_file in type_dir.glob('*.xml'):
                block_id = xml_file.stem
                display_name = ''

                try:
                    tree = ET.parse(xml_file)
                    root = tree.getroot()
                    display_name = root.get('display_name', '')
                except ET.ParseError:
                    pass

                block = OLXBlock(
                    block_id=block_id,
                    block_type=block_type,
                    display_name=display_name,
                    xml_file=str(xml_file.relative_to(self.course_root)),
                )

                result.blocks[block_id] = block

                if block_type not in result.block_types:
                    result.block_types[block_type] = []
                result.block_types[block_type].append(block_id)

        # Assign hierarchy context to blocks
        self._assign_hierarchy(
            result, chapter_map, sequential_map, vertical_map,
            chapter_children, sequential_children, vertical_children
        )

    def _parse_hierarchy_level(self, level_type: str) -> Dict[str, Dict]:
        """
        Parse all XML files in a hierarchy level directory.
        Returns a map of url_name -> {display_name, children: [(type, url_name)]}.
        """
        level_dir = self.course_root / level_type
        items = {}

        if not level_dir.is_dir():
            return items

        for xml_file in level_dir.glob('*.xml'):
            url_name = xml_file.stem
            display_name = ''
            children = []

            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()
                display_name = root.get('display_name', '')

                # Children are child elements with url_name attributes
                for child in root:
                    child_type = child.tag
                    child_url_name = child.get('url_name', '')
                    if child_url_name:
                        children.append((child_type, child_url_name))

            except ET.ParseError:
                pass

            items[url_name] = {
                'display_name': display_name,
                'children': children,
            }

        return items

    def _find_component_types(self) -> Set[str]:
        """Find all component type directories in the OLX export."""
        # Standard OLX component types
        known_types = {
            'problem', 'video', 'html', 'lti_consumer', 'lti',
            'openassessment', 'discussion', 'drag-and-drop-v2',
            'poll', 'survey', 'word_cloud', 'done', 'library_content',
            'split_test', 'conditional', 'annotatable',
        }

        # Also discover any directories that contain XML files
        found_types = set()
        skip_dirs = {'course', 'chapter', 'sequential', 'vertical',
                     'policies', 'static', 'tabs', 'drafts', 'about'}

        for item in self.course_root.iterdir():
            if item.is_dir() and item.name not in skip_dirs:
                # Check if it contains XML files (component definitions)
                if any(item.glob('*.xml')):
                    found_types.add(item.name)

        return found_types | {t for t in known_types if (self.course_root / t).is_dir()}

    def _assign_hierarchy(self, result: OLXParseResult,
                          chapter_map, sequential_map, vertical_map,
                          chapter_children, sequential_children, vertical_children):
        """Assign chapter/sequential/vertical context to each block."""
        for ch_id, ch_data in chapter_map.items():
            ch_name = ch_data['display_name']

            for seq_type, seq_id in ch_data.get('children', []):
                seq_data = sequential_map.get(seq_id, {})
                seq_name = seq_data.get('display_name', seq_id)

                for vert_type, vert_id in seq_data.get('children', []):
                    vert_data = vertical_map.get(vert_id, {})
                    vert_name = vert_data.get('display_name', vert_id)

                    for comp_type, comp_id in vert_data.get('children', []):
                        if comp_id in result.blocks:
                            result.blocks[comp_id].chapter = ch_name
                            result.blocks[comp_id].sequential = seq_name
                            result.blocks[comp_id].vertical = vert_name

    def cleanup(self):
        """Remove temporary extraction directory."""
        if self._temp_dir and os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
            self.extract_dir = None
            self.course_root = None
