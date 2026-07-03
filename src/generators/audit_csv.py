"""
Audit CSV Generator

Generates a detailed CSV report of the LTI link mapping results,
sorted with MISSING and NEW_ONLY items at the top for easy review.
"""

import csv
import io
import logging
import os
from typing import Optional

from src.processors.lti_mapper import MappingResult

logger = logging.getLogger(__name__)

# CSV column headers
CSV_COLUMNS = [
    'status',
    'resource_title',
    'old_lti_url',
    'new_lti_url',
    'block_id',
    'block_type',
    'edx_location',
    'notes',
]

# Sort order for status values (MISSING first, then NEW_ONLY, then MATCHED)
STATUS_SORT_ORDER = {
    'MISSING': 0,
    'NEW_ONLY': 1,
    'MATCHED': 2,
}


def generate_audit_csv(mapping_result: MappingResult, output_dir: str,
                       filename: str = 'lti_audit_report.csv') -> str:
    """
    Generate the audit CSV file.

    Args:
        mapping_result: The completed mapping result.
        output_dir: Directory to write the CSV file.
        filename: Output filename.

    Returns:
        Path to the generated CSV file.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)

    # Sort: MISSING first, then NEW_ONLY, then MATCHED
    sorted_links = sorted(
        mapping_result.mapped_links,
        key=lambda x: (STATUS_SORT_ORDER.get(x.status, 99), x.resource_title.lower(),
                       x.block_id, x.old_lti_url)
    )

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for link in sorted_links:
            writer.writerow({
                'status': link.status,
                'resource_title': link.resource_title,
                'old_lti_url': link.old_lti_url,
                'new_lti_url': link.new_lti_url,
                'block_id': link.block_id,
                'block_type': link.block_type,
                'edx_location': link.edx_location,
                'notes': link.notes,
            })

    logger.info(f'Generated audit CSV with {len(sorted_links)} entries: {output_path}')
    return output_path


def generate_audit_csv_string(mapping_result: MappingResult) -> str:
    """
    Generate the audit CSV as a string (for in-memory use).

    Args:
        mapping_result: The completed mapping result.

    Returns:
        CSV content as a string.
    """
    output = io.StringIO()

    sorted_links = sorted(
        mapping_result.mapped_links,
        key=lambda x: (STATUS_SORT_ORDER.get(x.status, 99), x.resource_title.lower(),
                       x.block_id, x.old_lti_url)
    )

    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()

    for link in sorted_links:
        writer.writerow({
            'status': link.status,
            'resource_title': link.resource_title,
            'old_lti_url': link.old_lti_url,
            'new_lti_url': link.new_lti_url,
            'block_id': link.block_id,
            'block_type': link.block_type,
            'edx_location': link.edx_location,
            'notes': link.notes,
        })

    return output.getvalue()
