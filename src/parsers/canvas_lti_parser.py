"""
Canvas IMSCC Parser for LTI Links

Parses .imscc (IMS Common Cartridge) files to extract all LTI External Tool
launch URLs and their associated metadata. IMSCC files are ZIP archives
containing XML conforming to the IMS Common Cartridge specification.

Key structures parsed:
- imsmanifest.xml: Resource manifest with module hierarchy
- */basiclti_link.xml or assignment XML: LTI launch URL definitions
- module_meta.xml: Module structure and item ordering
"""

import logging
import os
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# XML namespaces used in IMSCC / IMS Basic LTI
NAMESPACES = {
    'ims': 'http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1',
    'ims12': 'http://www.imsglobal.org/xsd/imsccv1p2/imscp_v1p2',
    'ims13': 'http://www.imsglobal.org/xsd/imsccv1p3/imscp_v1p3',
    'lom': 'http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource',
    'lomimscc': 'http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest',
    'blti': 'http://www.imsglobal.org/xsd/imsbasiclti_v1p0',
    'lticm': 'http://www.imsglobal.org/xsd/imslticm_v1p0',
    'lticp': 'http://www.imsglobal.org/xsd/imslticp_v1p0',
    'canvas': 'http://canvas.instructure.com/xsd/cccv1p0',
}

# Register all namespaces so they are preserved when rewriting XML
for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


@dataclass
class LTILink:
    """Represents a single LTI link found in a Canvas IMSCC export."""
    resource_id: str            # Identifier from imsmanifest.xml
    title: str                  # Display name of the LTI tool
    launch_url: str             # Full LTI launch URL
    description: str = ''       # Optional description
    module_title: str = ''      # Which Canvas module this lives in
    position: int = 0           # Position within the module
    xml_file: str = ''          # Path to the XML file within the archive
    # Parsed edX URL components
    edx_org: str = ''
    edx_course: str = ''
    edx_run: str = ''
    edx_block_type: str = ''
    edx_block_id: str = ''
    raw_course_id: str = ''     # Full course-v1:... string


@dataclass
class CanvasLTIParseResult:
    """Results from parsing an IMSCC file for LTI links."""
    lti_links: List[LTILink] = field(default_factory=list)
    course_title: str = ''
    total_resources: int = 0
    errors: List[str] = field(default_factory=list)


class CanvasLTIParser:
    """
    Parses a Canvas IMSCC export to extract all LTI External Tool links.

    Usage:
        parser = CanvasLTIParser(imscc_path)
        result = parser.parse()
        parser.cleanup()
    """

    def __init__(self, imscc_path: str):
        self.imscc_path = Path(imscc_path)
        self.extract_dir: Optional[Path] = None
        self._temp_dir: Optional[str] = None

    def parse(self) -> CanvasLTIParseResult:
        """
        Main entry point. Extracts the IMSCC ZIP and parses all LTI links.

        Returns:
            CanvasLTIParseResult with all discovered LTI links and metadata.
        """
        result = CanvasLTIParseResult()

        # Step 1: Extract ZIP
        self._temp_dir = tempfile.mkdtemp(prefix='canvas_lti_')
        self.extract_dir = Path(self._temp_dir) / 'extracted'

        try:
            with zipfile.ZipFile(self.imscc_path, 'r') as zf:
                zf.extractall(self.extract_dir)
        except zipfile.BadZipFile:
            result.errors.append('The uploaded .imscc file is not a valid ZIP archive.')
            return result
        except Exception as e:
            result.errors.append(f'Failed to extract .imscc file: {str(e)}')
            return result

        # Step 2: Parse imsmanifest.xml
        manifest_path = self.extract_dir / 'imsmanifest.xml'
        if not manifest_path.exists():
            result.errors.append('No imsmanifest.xml found in the .imscc archive.')
            return result

        try:
            tree = ET.parse(manifest_path)
            root = tree.getroot()
        except ET.ParseError as e:
            result.errors.append(f'Failed to parse imsmanifest.xml: {str(e)}')
            return result

        # Detect the IMS namespace version used
        ims_ns = self._detect_ims_namespace(root)

        # Step 3: Extract course title
        result.course_title = self._extract_course_title(root, ims_ns)

        # Step 4: Find all resources and identify LTI ones
        resources = root.findall(f'.//{{{ims_ns}}}resource') if ims_ns else root.findall('.//resource')
        result.total_resources = len(resources)

        # Build resource_id -> resource element map
        resource_map = {}
        for res in resources:
            rid = res.get('identifier', '')
            resource_map[rid] = res

        # Step 5: Build module structure for context
        module_items = self._parse_module_items(root, ims_ns)

        # Step 6: Extract LTI links from resources
        for res in resources:
            res_type = res.get('type', '')
            res_id = res.get('identifier', '')

            # LTI resources have types containing 'imsbasiclti' or 'basiclti'
            if 'imsbasiclti' in res_type.lower() or 'basiclti' in res_type.lower():
                lti_link = self._extract_lti_from_resource(res, res_id, ims_ns)
                if lti_link:
                    # Add module context
                    if res_id in module_items:
                        lti_link.module_title = module_items[res_id].get('module_title', '')
                        lti_link.position = module_items[res_id].get('position', 0)
                    result.lti_links.append(lti_link)

            # Also check for assignments that reference LTI tools
            elif 'assignment' in res_type.lower() or 'associatedcontent' in res_type.lower():
                lti_link = self._check_assignment_for_lti(res, res_id, ims_ns)
                if lti_link:
                    if res_id in module_items:
                        lti_link.module_title = module_items[res_id].get('module_title', '')
                        lti_link.position = module_items[res_id].get('position', 0)
                    result.lti_links.append(lti_link)

        # Step 7: Also scan module items for LTI tool references not in resources
        self._scan_module_items_for_lti(root, ims_ns, resource_map, result, module_items)

        logger.info(f'Found {len(result.lti_links)} LTI links in Canvas export')
        return result

    def _detect_ims_namespace(self, root: ET.Element) -> str:
        """Detect which version of the IMS namespace is used."""
        tag = root.tag
        if '{' in tag:
            ns = tag.split('}')[0].strip('{')
            return ns
        # Try known namespaces
        for ns_key in ['ims', 'ims12', 'ims13']:
            ns = NAMESPACES[ns_key]
            if root.findall(f'.//{{{ns}}}resource'):
                return ns
        return ''

    def _extract_course_title(self, root: ET.Element, ims_ns: str) -> str:
        """Extract course title from manifest metadata."""
        # Try LOM metadata title
        for ns_key in ['lomimscc', 'lom']:
            ns = NAMESPACES[ns_key]
            title_el = root.find(f'.//{{{ns}}}title/{{{ns}}}string')
            if title_el is not None and title_el.text:
                return title_el.text.strip()

        # Fallback: try schema title
        if ims_ns:
            title_el = root.find(f'.//{{{ims_ns}}}title')
            if title_el is not None and title_el.text:
                return title_el.text.strip()

        return 'Unknown Course'

    def _parse_module_items(self, root: ET.Element, ims_ns: str) -> Dict[str, Dict]:
        """
        Parse the organization/module structure to map resource identifiers
        to their module context (module title, position).
        """
        items = {}

        # Parse organizations section
        if ims_ns:
            orgs = root.findall(f'.//{{{ims_ns}}}organization')
        else:
            orgs = root.findall('.//organization')

        for org in orgs:
            if ims_ns:
                top_items = org.findall(f'{{{ims_ns}}}item')
            else:
                top_items = org.findall('item')

            for module_item in top_items:
                module_title = ''
                if ims_ns:
                    title_el = module_item.find(f'{{{ims_ns}}}title')
                else:
                    title_el = module_item.find('title')
                if title_el is not None and title_el.text:
                    module_title = title_el.text.strip()

                # Get child items (actual content references)
                if ims_ns:
                    children = module_item.findall(f'{{{ims_ns}}}item')
                else:
                    children = module_item.findall('item')

                for pos, child in enumerate(children):
                    identifierref = child.get('identifierref', '')
                    if identifierref:
                        child_title = ''
                        if ims_ns:
                            ct = child.find(f'{{{ims_ns}}}title')
                        else:
                            ct = child.find('title')
                        if ct is not None and ct.text:
                            child_title = ct.text.strip()

                        items[identifierref] = {
                            'module_title': module_title,
                            'position': pos + 1,
                            'title': child_title,
                        }

        # Also parse module_meta.xml if it exists (Canvas-specific)
        module_meta_path = self.extract_dir / 'course_settings' / 'module_meta.xml'
        if module_meta_path.exists():
            self._parse_canvas_module_meta(module_meta_path, items)

        return items

    def _parse_canvas_module_meta(self, meta_path: Path, items: Dict[str, Dict]):
        """Parse Canvas-specific module_meta.xml for additional context."""
        try:
            tree = ET.parse(meta_path)
            root = tree.getroot()
            canvas_ns = NAMESPACES['canvas']

            for module in root.findall(f'.//{{{canvas_ns}}}module'):
                mod_title_el = module.find(f'{{{canvas_ns}}}title')
                mod_title = mod_title_el.text.strip() if mod_title_el is not None and mod_title_el.text else ''

                for pos, item in enumerate(module.findall(f'.//{{{canvas_ns}}}item')):
                    content_type = ''
                    identifierref = ''
                    item_title = ''

                    ct_el = item.find(f'{{{canvas_ns}}}content_type')
                    if ct_el is not None and ct_el.text:
                        content_type = ct_el.text.strip()

                    iref_el = item.find(f'{{{canvas_ns}}}identifierref')
                    if iref_el is not None and iref_el.text:
                        identifierref = iref_el.text.strip()

                    title_el = item.find(f'{{{canvas_ns}}}title')
                    if title_el is not None and title_el.text:
                        item_title = title_el.text.strip()

                    url_el = item.find(f'{{{canvas_ns}}}url')
                    url = url_el.text.strip() if url_el is not None and url_el.text else ''

                    if identifierref:
                        items[identifierref] = {
                            'module_title': mod_title,
                            'position': pos + 1,
                            'title': item_title,
                            'content_type': content_type,
                            'url': url,
                        }

        except Exception as e:
            logger.warning(f'Could not parse module_meta.xml: {e}')

    def _extract_lti_from_resource(self, resource: ET.Element, res_id: str,
                                     ims_ns: str) -> Optional[LTILink]:
        """
        Extract LTI link data from a basiclti_link resource.
        Looks for the associated XML file and parses the launch URL.
        """
        # Find the XML file referenced by this resource
        xml_file = None
        if ims_ns:
            file_els = resource.findall(f'{{{ims_ns}}}file')
        else:
            file_els = resource.findall('file')

        for f in file_els:
            href = f.get('href', '')
            if href.endswith('.xml'):
                xml_file = href
                break

        # If no file element, check the href attribute on the resource itself
        if not xml_file:
            href = resource.get('href', '')
            if href and href.endswith('.xml'):
                xml_file = href
            elif href:
                # Some exports use directories; look for basiclti_link.xml inside
                possible = self.extract_dir / href
                if possible.is_dir():
                    for candidate in ['basiclti_link.xml', 'basic_lti_link.xml']:
                        if (possible / candidate).exists():
                            xml_file = f'{href}/{candidate}'
                            break

        if not xml_file:
            # Try common patterns
            for pattern in [
                f'{res_id}/basiclti_link.xml',
                f'{res_id}/basic_lti_link.xml',
                f'{res_id}.xml',
            ]:
                if (self.extract_dir / pattern).exists():
                    xml_file = pattern
                    break

        if not xml_file:
            logger.warning(f'No XML file found for LTI resource {res_id}')
            return None

        xml_path = self.extract_dir / xml_file
        if not xml_path.exists():
            logger.warning(f'XML file {xml_file} not found on disk for resource {res_id}')
            return None

        return self._parse_lti_xml(xml_path, res_id, xml_file)

    def _parse_lti_xml(self, xml_path: Path, res_id: str, xml_file: str) -> Optional[LTILink]:
        """Parse a basiclti_link.xml file to extract the launch URL and metadata."""
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f'Failed to parse {xml_path}: {e}')
            return None

        blti_ns = NAMESPACES['blti']

        # Extract launch URL
        launch_url = ''
        launch_el = root.find(f'{{{blti_ns}}}launch_url')
        if launch_el is None:
            # Try without namespace
            launch_el = root.find('launch_url')
        if launch_el is None:
            # Try secure_launch_url
            launch_el = root.find(f'{{{blti_ns}}}secure_launch_url')
        if launch_el is None:
            launch_el = root.find('secure_launch_url')

        if launch_el is not None and launch_el.text:
            launch_url = launch_el.text.strip()

        # Also check for URL in custom/extension properties
        if not launch_url:
            launch_url = self._find_url_in_extensions(root)

        if not launch_url:
            logger.warning(f'No launch URL found in {xml_path}')
            return None

        # Extract title
        title = ''
        title_el = root.find(f'{{{blti_ns}}}title')
        if title_el is None:
            title_el = root.find('title')
        if title_el is not None and title_el.text:
            title = title_el.text.strip()

        # Extract description
        desc = ''
        desc_el = root.find(f'{{{blti_ns}}}description')
        if desc_el is None:
            desc_el = root.find('description')
        if desc_el is not None and desc_el.text:
            desc = desc_el.text.strip()

        link = LTILink(
            resource_id=res_id,
            title=title,
            launch_url=launch_url,
            description=desc,
            xml_file=xml_file,
        )

        # Parse edX components from the URL
        self._parse_edx_url(link)

        return link

    def _find_url_in_extensions(self, root: ET.Element) -> str:
        """Search extension properties for a launch URL."""
        lticm_ns = NAMESPACES['lticm']

        # Check blti:extensions > lticm:property
        for ext in root.iter():
            if 'property' in ext.tag.lower():
                name = ext.get('name', '')
                if name in ('url', 'launch_url', 'tool_id') and ext.text:
                    text = ext.text.strip()
                    if text.startswith('http'):
                        return text

        # Check custom parameters
        for custom in root.iter():
            if 'custom' in custom.tag.lower() or 'property' in custom.tag.lower():
                if custom.text and 'http' in custom.text:
                    text = custom.text.strip()
                    if 'edx.org' in text or 'mitx' in text.lower() or 'lti' in text.lower():
                        return text

        return ''

    def _check_assignment_for_lti(self, resource: ET.Element, res_id: str,
                                    ims_ns: str) -> Optional[LTILink]:
        """
        Check if an assignment resource contains an LTI external tool submission.
        Canvas assignments can wrap LTI tools.
        """
        # Find associated files
        if ims_ns:
            file_els = resource.findall(f'{{{ims_ns}}}file')
        else:
            file_els = resource.findall('file')

        for f in file_els:
            href = f.get('href', '')
            if not href:
                continue

            xml_path = self.extract_dir / href
            if not xml_path.exists():
                continue

            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()

                # Look for external_tool_url or similar in assignment XML
                canvas_ns = NAMESPACES['canvas']
                for el in root.iter():
                    tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                    if tag in ('external_tool_url', 'url', 'external_tool_tag_attributes'):
                        if el.text and ('edx.org' in el.text or 'lti' in el.text.lower()):
                            url = el.text.strip()
                            title_el = root.find(f'{{{canvas_ns}}}title')
                            if title_el is None:
                                title_el = root.find('title')
                            title = title_el.text.strip() if title_el is not None and title_el.text else res_id

                            link = LTILink(
                                resource_id=res_id,
                                title=title,
                                launch_url=url,
                                xml_file=href,
                            )
                            self._parse_edx_url(link)
                            return link

            except ET.ParseError:
                continue

        return None

    def _scan_module_items_for_lti(self, root: ET.Element, ims_ns: str,
                                     resource_map: Dict, result: CanvasLTIParseResult,
                                     module_items: Dict):
        """
        Scan module_meta.xml for ContextExternalTool items that might not
        appear as basiclti_link resources in the manifest.
        """
        meta_path = self.extract_dir / 'course_settings' / 'module_meta.xml'
        if not meta_path.exists():
            return

        existing_ids = {link.resource_id for link in result.lti_links}
        canvas_ns = NAMESPACES['canvas']

        try:
            tree = ET.parse(meta_path)
            meta_root = tree.getroot()

            for module in meta_root.findall(f'.//{{{canvas_ns}}}module'):
                mod_title_el = module.find(f'{{{canvas_ns}}}title')
                mod_title = mod_title_el.text.strip() if mod_title_el is not None and mod_title_el.text else ''

                for pos, item in enumerate(module.findall(f'.//{{{canvas_ns}}}item')):
                    ct_el = item.find(f'{{{canvas_ns}}}content_type')
                    content_type = ct_el.text.strip() if ct_el is not None and ct_el.text else ''

                    if content_type != 'ContextExternalTool':
                        continue

                    iref_el = item.find(f'{{{canvas_ns}}}identifierref')
                    identifierref = iref_el.text.strip() if iref_el is not None and iref_el.text else ''

                    if identifierref in existing_ids:
                        continue

                    title_el = item.find(f'{{{canvas_ns}}}title')
                    item_title = title_el.text.strip() if title_el is not None and title_el.text else ''

                    url_el = item.find(f'{{{canvas_ns}}}url')
                    url = url_el.text.strip() if url_el is not None and url_el.text else ''

                    if url and ('edx.org' in url or 'edx' in url.lower() or 'lti' in url.lower()):
                        link = LTILink(
                            resource_id=identifierref or f'module_item_{pos}',
                            title=item_title,
                            launch_url=url,
                            module_title=mod_title,
                            position=pos + 1,
                        )
                        self._parse_edx_url(link)
                        result.lti_links.append(link)
                        existing_ids.add(link.resource_id)

        except Exception as e:
            logger.warning(f'Error scanning module_meta.xml for LTI items: {e}')

    def _parse_edx_url(self, link: LTILink):
        """
        Parse an edX LTI URL to extract org, course, run, block_type, and block_id.

        Common URL patterns:
        - https://courses.edx.org/courses/course-v1:MITx+1.000+2025_Spring/...
        - Contains block-v1:MITx+1.000+2025_Spring+type@problem+block@abc123
        - Or /xblock/block-v1:... in the path
        """
        url = link.launch_url

        # Pattern 1: course-v1:{org}+{course}+{run}
        course_match = re.search(r'course-v1:([^/+]+)\+([^/+]+)\+([^/+\s?#]+)', url)
        if course_match:
            link.edx_org = course_match.group(1)
            link.edx_course = course_match.group(2)
            link.edx_run = course_match.group(3)
            link.raw_course_id = f'course-v1:{link.edx_org}+{link.edx_course}+{link.edx_run}'

        # Pattern 2: block-v1:{org}+{course}+{run}+type@{type}+block@{id}
        block_match = re.search(
            r'block-v1:([^/+]+)\+([^/+]+)\+([^/+]+)\+type@([^/+]+)\+block@([^/+\s?#&]+)',
            url
        )
        if block_match:
            link.edx_org = block_match.group(1)
            link.edx_course = block_match.group(2)
            link.edx_run = block_match.group(3)
            link.edx_block_type = block_match.group(4)
            link.edx_block_id = block_match.group(5)
            link.raw_course_id = f'course-v1:{link.edx_org}+{link.edx_course}+{link.edx_run}'

        # Pattern 3: usage key in query string
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for param in ['usage_key', 'id', 'block_id']:
            if param in qs:
                val = qs[param][0]
                if 'block-v1:' in val:
                    bm = re.search(
                        r'block-v1:([^+]+)\+([^+]+)\+([^+]+)\+type@([^+]+)\+block@(.+)',
                        val
                    )
                    if bm:
                        link.edx_org = bm.group(1)
                        link.edx_course = bm.group(2)
                        link.edx_run = bm.group(3)
                        link.edx_block_type = bm.group(4)
                        link.edx_block_id = bm.group(5)

    def get_extract_dir(self) -> Optional[Path]:
        """Return the extraction directory path for downstream use."""
        return self.extract_dir

    def cleanup(self):
        """Remove temporary extraction directory."""
        if self._temp_dir and os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
            self.extract_dir = None
