# Layout reference sources

What was consulted to build the layout vocabulary, the synthetic test renderer and the evaluation cases.
No screenshot from any of these sources is stored in this repository, used for training, or redistributed.
No model was trained; the perception layer is accessibility + OCR geometry + spatial rules (see
`src/studio_monitor/perception`). Retrieval date for all entries: **2026-10-09**.

| # | Source | Retrieved | Studio version / language | Layout / state shown | Usage & licence | Imagery reuse |
|---|---|---|---|---|---|---|
| 1 | TikTok LIVE Studio Help Center — "Learn the basics of LIVE" https://www.tiktok.com/live/studio/help/article/Get-started-with-your-first-LIVE/Learn-the-basics-of-LIVE | 2026-10-09 (page "Updated on Aug 15, 2025"), English | not stated | Describes the panels: *Add scene* / *Add source* (up to 20 sources per scene), scene customisation area, *Mobile preview*, *LIVE Info* (topics, description, cover), *Audio Mixer* (add/configure audio devices, adjust volume), *LIVE Chat* (comments, gifts, joins, likes, follows, subscriptions), metrics (unique viewers, followers, likes; CPU, memory, network, frame drop, frame rate), *Go LIVE* button; one UI diagram hosted on tiktokcdn.com | TikTok terms of service; text used only as vocabulary reference for anchor words | **No** — the diagram was not downloaded or stored |
| 2 | TikTok LIVE Studio Help Center — "TikTok LIVE Studio Operation Manual" https://www.tiktok.com/live/studio/help/article/1023/tiktok-live-studio-operation-manual_en-US?lang=en | 2026-10-09 | — | Page content is loaded dynamically; the retrievable HTML only contained the download link, so no layout information could be taken from it | TikTok terms of service | n/a |
| 3 | TikTok Creator Academy — "LIVE Studio tools" https://www.tiktok.com/creator-academy/article/LIVE-Studio-tools | 2026-10-09 | — | Only navigation was retrievable programmatically; not used | TikTok terms of service | n/a |
| 4 | Third-party guides found by search (metricool.com, tikfinity blog, influenceflow.io, buildmyplays.com) | 2026-10-09 | various | Prose descriptions of the Sources/Scenes panel, LIVE information, LIVE data and comments, control panel with Record, Mixer and Microphone | Copyrighted articles; not fetched, not used beyond confirming panel names already visible in the operator's own Studio | **No** |
| 5 | Operator's own Studio window on this PC (WGC capture, 1512×726, English, Studio build with "Lets Go LIVE!" title chip, not live) | 2026-10-09 | version string not exposed by the window; language English | Left Studio-view/sources/Tools panel, centre portrait program preview with an audio-device warning banner, control bar with sliders + green level segment + red *Go LIVE*, right *LIVE performance* / *LIVE chat* panels, status row (CPU, Memory, Upload, Frame drops, FPS) | Private; stored only under `tests/fixtures/private/` (git-ignored) | **No** |
| 6 | Operator's screenshot of the *End streaming?* confirmation dialog | 2026-10-09 | English | Modal with heading, body, *End now* / *Cancel* | Private; git-ignored | **No** |
| 7 | Microsoft OmniParser repository and model card https://github.com/microsoft/OmniParser , https://huggingface.co/microsoft/OmniParser-v2.0 | 2026-10-09 | v2.0 | Licence review and benchmark only (see OMNIPARSER_EVALUATION.md) | Code CC-BY-4.0; `icon_detect` AGPL-3.0; `icon_detect_v3` and `icon_caption` MIT | weights not redistributed here |
| 8 | Microsoft "Application loopback audio capture" sample https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/ | 2026-10-09 | Windows 10 build 20348+ | API reference for process loopback | MIT sample code; re-implemented in Python (`audio/process_loopback.py`) | n/a |

## Evaluation cases derived from these sources

- Synthetic renderer `tests/studio_synth.py`: parameterised from the panel list in #1 and the geometry observed in #5
  (sizes 1280×720 … 2560×1440, DPI-like scale 0.9–1.5, swapped panels, missing right panel, black/portrait/full
  preview, thumbnails and chat avatars with face markers, banner, modal, dark/lit meter).
- Real evidence: `tests/test_perception_real.py` runs Windows OCR + discovery on #5 and #6 when the private fixtures
  are present on the development PC (skipped elsewhere).

## Tested combinations (declared)

| Dimension | Tested | Not tested |
|---|---|---|
| Window size | 1280×720, 1512×726 (real), 1920×1080, 2560×1440 (synthetic) | ultrawide, portrait monitors |
| DPI / scale | 0.9, 1.0 (real, 100 %), 1.25, 1.5 (synthetic) | 175 %+ |
| Theme | dark (real + synthetic) | light theme |
| Panel arrangement | default, swapped, right panel closed (synthetic) | floating/undocked panels |
| Language | English anchors (real + synthetic) | any other language (anchor vocabulary is English-only; discovery degrades to "partly") |
| Studio state | not live (real); live badge/timer (synthetic) | live (real) |
