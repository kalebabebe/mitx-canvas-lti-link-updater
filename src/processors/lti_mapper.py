"""
LTI Link Mapper

Takes parsed LTI links from a Canvas export and a block inventory from an
edX OLX export, then validates and maps each link to its corresponding
block in the new course. Produces categorized results (MATCHED, MISSING,
NEW_ONLY) for the audit report and updated IMSCC generation.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.parsers.canvas_lti_parser import LTILink
from src.parsers.olx_parser import OLXBlock, OLXParseResult

logger = logging.getLogger(__name__)


@dataclass
class MappedLink:
    """A single LTI link with its mapping status and updated URL."""
    status: str                 # MATCHED, MISSING, NEW_ONLY
    resource_title: str = ''
    old_lti_url: str = ''
    new_lti_url: str = ''
    block_id: str = ''
    block_type: str = ''
    edx_location: str = ''      # chapter > sequential > vertical
    notes: str = ''
    # Internal references for IMSCC update
    resource_id: str = ''       # Canvas resource identifier
    xml_file: str = ''          # Path to XML file in IMSCC


@dataclass
class MappingResult:
    """Complete mapping results."""
    mapped_links: List[MappedLink] = field(default_factory=list)
    matched_count: int = 0
    missing_count: int = 0
    new_only_count: int = 0
    total_lti_links: int = 0
    old_course_id: str = ''
    new_course_id: str = ''
    warnings: List[str] = field(default_factory=list)


class LTIMapper:
    """
    Maps LTI links from a Canvas IMSCC export to blocks in a new edX course.

    The core logic:
    1. For each LTI link in Canvas, extract the block ID from the URL.
    2. Look up that block ID in the new edX course's block inventory.
    3. If found (MATCHED), construct the new URL by replacing the old course
       run with the new run, keeping the block ID the same.
    4. If not found (MISSING), flag for manual review.
    5. For blocks in the new course with no Canvas link (NEW_ONLY), flag
       for awareness.
    """

    def __init__(self, lti_links: List[LTILink], olx_result: OLXParseResult):
        self.lti_links = lti_links
        self.olx = olx_result

    def map(self) -> MappingResult:
        """
        Perform the mapping of old LTI links to new course blocks.

        Returns:
            MappingResult with categorized links and statistics.
        """
        result = MappingResult()
        result.total_lti_links = len(self.lti_links)
        result.new_course_id = self.olx.course_id

        # Track which block IDs are referenced by Canvas LTI links
        referenced_block_ids = set()

        # Determine the old course ID from the first LTI link with a parsed course ID
        for link in self.lti_links:
            if link.raw_course_id:
                result.old_course_id = link.raw_course_id
                break

        # Phase 1: Map each Canvas LTI link
        for link in self.lti_links:
            mapped = self._map_single_link(link)
            result.mapped_links.append(mapped)

            if mapped.status == 'MATCHED':
                result.matched_count += 1
                referenced_block_ids.add(mapped.block_id)
            elif mapped.status == 'MISSING':
                result.missing_count += 1

        # Phase 2: Find NEW_ONLY blocks (in new course but not referenced by Canvas)
        # Only report linkable block types (Canvas LTI links launch sequentials);
        # listing every problem/video/html block would bury the useful rows.
        linkable_types = {'sequential', 'vertical'}
        for block_id, block in self.olx.blocks.items():
            if block.block_type not in linkable_types:
                continue
            if block_id not in referenced_block_ids:
                location = self._build_location(block)
                result.mapped_links.append(MappedLink(
                    status='NEW_ONLY',
                    resource_title=block.display_name or block_id,
                    old_lti_url='',
                    new_lti_url='',
                    block_id=block_id,
                    block_type=block.block_type,
                    edx_location=location,
                    notes='Block exists in new edX course but has no Canvas LTI link',
                ))
                result.new_only_count += 1

        # Warnings — wrong-file-pairing checks first, since they explain
        # everything else on the report.
        if result.old_course_id and result.old_course_id == result.new_course_id:
            result.warnings.append(
                'The Canvas links already point to this exact edX course run, so '
                'the output will be identical to the input. If you meant to move '
                'links to a NEW run, check that you exported the OLD Canvas course '
                'and the NEW edX course.'
            )

        link_orgs = {l.edx_org for l in self.lti_links if l.edx_org}
        if link_orgs and self.olx.org and self.olx.org not in link_orgs:
            result.warnings.append(
                f'None of the Canvas LTI links reference the organization '
                f'"{self.olx.org}" found in the edX export (links reference: '
                f'{", ".join(sorted(link_orgs))}). This usually means the wrong '
                f'edX course was exported.'
            )

        if result.missing_count > 0:
            result.warnings.append(
                f'{result.missing_count} LTI link(s) reference blocks not found in the '
                f'new edX course. Their URLs are left unchanged in the updated export — '
                f'fix or remove those links manually in Canvas after importing.'
            )

        if not result.old_course_id:
            result.warnings.append(
                'Could not determine the old edX course ID from the LTI URLs. '
                'URL replacement may not work correctly.'
            )

        logger.info(
            f'Mapping complete: {result.matched_count} matched, '
            f'{result.missing_count} missing, {result.new_only_count} new-only'
        )
        return result

    def _map_single_link(self, link: LTILink) -> MappedLink:
        """Map a single LTI link to the new course."""
        block_id = link.edx_block_id
        block_type = link.edx_block_type

        # If no block ID was parsed from the URL, it might be a course-level link
        if not block_id:
            return MappedLink(
                status='MISSING' if not self._is_course_level_url(link.launch_url) else 'MATCHED',
                resource_title=link.title,
                old_lti_url=link.launch_url,
                new_lti_url=self._update_course_level_url(link.launch_url) if self._is_course_level_url(link.launch_url) else '',
                block_id='',
                block_type='',
                edx_location='',
                notes='Course-level URL (no specific block)' if self._is_course_level_url(link.launch_url) else 'Could not parse block ID from URL',
                resource_id=link.resource_id,
                xml_file=link.xml_file,
            )

        # Look up the block ID in the new course
        block = self.olx.blocks.get(block_id)

        if block:
            # MATCHED: construct the new URL
            new_url = self._construct_new_url(link)
            location = self._build_location(block)

            return MappedLink(
                status='MATCHED',
                resource_title=link.title or block.display_name,
                old_lti_url=link.launch_url,
                new_lti_url=new_url,
                block_id=block_id,
                block_type=block.block_type or block_type,
                edx_location=location,
                notes='',
                resource_id=link.resource_id,
                xml_file=link.xml_file,
            )
        else:
            # MISSING: block not found in new course
            return MappedLink(
                status='MISSING',
                resource_title=link.title,
                old_lti_url=link.launch_url,
                new_lti_url='',
                block_id=block_id,
                block_type=block_type,
                edx_location='',
                notes='Block ID not found in new edX course',
                resource_id=link.resource_id,
                xml_file=link.xml_file,
            )

    def _construct_new_url(self, link: LTILink) -> str:
        """
        Construct the new LTI URL by replacing the old course identifiers
        with the new course's identifiers while preserving the block ID.
        """
        url = link.launch_url
        new_org = self.olx.org
        new_course = self.olx.course
        new_run = self.olx.run

        # Replace course-v1:{old_org}+{old_course}+{old_run} with new values
        old_course_pattern = re.compile(
            r'course-v1:' + re.escape(link.edx_org) +
            r'\+' + re.escape(link.edx_course) +
            r'\+' + re.escape(link.edx_run)
        )
        new_course_str = f'course-v1:{new_org}+{new_course}+{new_run}'
        url = old_course_pattern.sub(new_course_str, url)

        # Replace block-v1:{old}+type@...+block@... with new course prefix
        old_block_pattern = re.compile(
            r'block-v1:' + re.escape(link.edx_org) +
            r'\+' + re.escape(link.edx_course) +
            r'\+' + re.escape(link.edx_run)
        )
        new_block_prefix = f'block-v1:{new_org}+{new_course}+{new_run}'
        url = old_block_pattern.sub(new_block_prefix, url)

        return url

    def _is_course_level_url(self, url: str) -> bool:
        """Check if a URL points to the course level (not a specific block)."""
        if not url:
            return False
        # Course-level URLs have a course ID but no block reference
        has_course = bool(re.search(r'course-v1:', url))
        has_block = bool(re.search(r'block-v1:|block@', url))
        # Also consider URLs ending in /courseware/ or /course/ as course-level
        is_courseware = bool(re.search(r'/courseware/?$|/course/?$|/about/?$', url))
        return has_course and (not has_block or is_courseware)

    def _update_course_level_url(self, url: str) -> str:
        """Update a course-level URL to point to the new course."""
        course_match = re.search(r'course-v1:([^/+]+)\+([^/+]+)\+([^/+\s?#]+)', url)
        if course_match:
            old_id = course_match.group(0)
            new_id = f'course-v1:{self.olx.org}+{self.olx.course}+{self.olx.run}'
            return url.replace(old_id, new_id)
        return url

    def _build_location(self, block: OLXBlock) -> str:
        """Build a human-readable location string for a block."""
        parts = []
        if block.chapter:
            parts.append(block.chapter)
        if block.sequential:
            parts.append(block.sequential)
        if block.vertical:
            parts.append(block.vertical)
        return ' > '.join(parts) if parts else ''
