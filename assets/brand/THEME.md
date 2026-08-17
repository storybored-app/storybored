# StoryBored brand sheet

The mark is a storyboard: a 2×2 grid of panels, three sitting empty ("bored"),
one lit amber with a play cut-out — the shot that comes alive. One concept,
used everywhere: app icon, favicon, lockups, banner.

## Palette

Aligned 1:1 with the app's Tailwind tokens in `frontend/src/styles.css`.

| Role                              | Token         | Hex       |
| --------------------------------- | ------------- | --------- |
| Ground (icon tile, dark surfaces) | `ink-950`     | `#0a0a0c` |
| Wordmark "Story" on light         | `ink-900`     | `#101014` |
| Tile border, quiet strokes        | `line-bright` | `#34343d` |
| Empty panels, secondary text      | `fog`         | `#8b8a94` |
| Wordmark "Story" on dark          | `paper`       | `#ece9e2` |
| Accent — lit panel, "Bored"       | `amber-450`   | `#f0b429` |
| Accent on light backgrounds       | `amber-550`   | `#de911d` |

Empty panels are stroked `fog` at 50–60% opacity so the lit panel always wins.
Amber is the only accent; never introduce a second hue.

## Typography

- Wordmark: **Inter SemiBold (600)**, tracking −0.02 em, "Story" in
  `paper` (dark) / `ink-900` (light), "Bored" in `amber-450` / `amber-550`.
  The SVG wordmark/lockups carry Inter outlines as paths — no font needed at
  render time. In-app, render it as live text (the app already ships Inter).
- Supporting copy: Inter Regular/Medium. Secondary text in `fog`.

## Files

| File               | Use                                              |
| ------------------ | ------------------------------------------------ |
| `icon.svg`         | Square app icon, 512 px master (works to ~32 px) |
| `favicon.svg`      | Tiny-size variant: thicker strokes, no triangle  |
| `wordmark.svg`     | Type-only, for dark backgrounds                  |
| `lockup-dark.svg`  | Icon + wordmark, dark backgrounds                |
| `lockup-light.svg` | Icon + wordmark, light backgrounds               |
| `banner.svg`       | 1280×640 GitHub social preview                   |

## Rules

- **Clear space:** keep one lit-panel width (¼ of the icon side) empty around
  the icon; half the tile height around lockups.
- **Minimum sizes:** icon 32 px (below that, use `favicon.svg`); lockups 24 px
  tall; wordmark 16 px tall.
- **Do:** use the dark lockup on any `ink-*` surface; the light lockup on
  white/paper; keep the lit panel top-left.
- **Don't:** recolor the panels, add gradients or shadows, rotate the mark,
  put the dark lockup on mid-gray backgrounds, set the wordmark in another
  font, or write "Storybored"/"StoryBoard" — it's **StoryBored**.
