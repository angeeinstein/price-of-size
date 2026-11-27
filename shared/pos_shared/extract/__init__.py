"""Extraction: raw evidence (snippets) -> structured candidates with provenance.

Pure and versioned (EXTRACT_VERSION): re-running it over stored raw evidence is
how derived data is rebuilt. Nothing here decides which candidate wins; that is
normalization's job, and every candidate keeps its method and confidence.
"""

EXTRACT_VERSION = "3"
