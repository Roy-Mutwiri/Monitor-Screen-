"""Stream-health detectors evaluated on fresh, valid Studio frames.

Each detector reports a *condition* with temporal confirmation; invalid or
stale frames yield UNKNOWN rather than a problem. The suite converts
sustained conditions into incidents with cautious wording.
"""
from .suite import Condition, DetectorSuite, DetectorsConfig, SuiteOutput  # noqa: F401
