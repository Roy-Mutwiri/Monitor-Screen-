"""Automatic perception of the Studio window: layout discovery from
accessibility + OCR word boxes + visual anchors, continuous relocalization,
spatial grouping of dialogs/banners, and an optional external screen parser.

Nothing in here is a hard-coded percentage rectangle: every element carries
its evidence source and confidence, and cached relative coordinates are only
written after an evidence-based discovery and are revalidated against the
current frame before use.
"""
from .layout import Layout, LayoutElement, LayoutStore, discover_layout, layout_signature  # noqa: F401
from .tracker import LayoutTracker, PerceptionStatus  # noqa: F401
