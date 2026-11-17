"""Clip captions: generation from transcripts, styles and ASS subtitle output.

- `styles.py` — caption style presets, bounds and color handling.
- `generate.py` — builds clip-relative caption lines from transcript segments.
- `ass.py` — renders captions and a style as an ASS (Advanced SubStation
  Alpha) subtitle file for burning into renders.

Caption times are seconds relative to the clip start.
"""
