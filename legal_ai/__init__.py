"""Local, private and traceable legal-study corpus tooling."""

__version__ = "0.2.0"

# `p1-v1` produced the immutable embedded-text extraction records currently
# stored in data/raw/pages.  v2 may consume those records, but never treats a
# p1 processed index as if it already contained the new provenance fields.
PIPELINE_VERSION = "p2-ocr-v1"
LEGACY_RAW_PIPELINE_VERSIONS = frozenset({"p1-v1"})
