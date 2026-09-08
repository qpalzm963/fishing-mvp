# Vision fixtures

These five 540×1170 JPEG frames are small regression fixtures extracted from
`1000018497.mp4` at representative UI moments:

- `waiting.jpg` — 6.00s, inactive action control
- `prompt.jpg` — 6.70s, purple prompt banner
- `qte.jpg` — 18.63s, QTE gauge with marker and target colours
- `quality.jpg` — 21.57s, quality flash over the gauge
- `result.jpg` — 23.66s, dark result overlay

The full source video and generated annotated outputs stay outside the
repository. The fixtures are intentionally downscaled so the tests also
exercise the detector's resolution-independent geometry.
