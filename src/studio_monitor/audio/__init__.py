"""Automatic audio discovery and real-time listening.

Measurement sources, in order of preference, each labelled honestly:
  studio_loopback   Windows process loopback of Studio's rendered audio (what Studio plays locally; may exclude
                    microphone/input audio that Studio uploads but does not play) — samples, VAD, optional transcription
  studio_session    Studio's audio-session peak meter (no samples: level only, no speech detection)
  input_device      an explicitly identified input endpoint matched from Studio's own labels (samples)
  visual_meter      Studio's on-screen meter (existing detector)
  unavailable       association uncertain or unsupported
Nothing here changes routing, unmutes devices or enables monitoring.
"""
from .resolver import AudioBinding, AudioSourceResolver  # noqa: F401
from .levels import AudioAnalyzer, AudioReadingState  # noqa: F401
