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

Four 1080×2340 fixtures from
`Screen_Recording_20260921_223703_Clash of Critters.mp4` cover late QTEs:

- `cool_20260921.jpg` — 7.50s, genuine Cool lettering
- `qte_splash_16s.jpg` — 16.25s, blue splash without a quality label
- `qte_splash_20s.jpg` — 20.27s, narrow blue water highlight
- `qte_splash_21s.jpg` — 21.08s, water effect near a target crossing

The tests exercise these frames at both 540px and 1080px widths. The user
identified the portion after 16s as requiring manual intervention; visible
success animations in that portion are not evidence of automated success.
