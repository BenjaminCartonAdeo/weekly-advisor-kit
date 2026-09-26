# Diagrammes

Source unique : 5 fichiers `*.html` stylés (v6.2, CSS Geist/Instrument Serif) avec SVG inline.
Chaque HTML est la référence éditable ; les exports image sont régénérés à l'identique.

## Fichiers

| HTML (source) | SVG (vectoriel) | PNG (bitmap) | viewBox |
|---|---|---|---|
| `architecture.html` (54.5K) | `architecture.svg` (51.2K) | `architecture.png` (338K, 1600×2600) | 0 0 1560 2512 |
| `classes.html` (50.0K) | `classes.svg` (47.5K) | `classes.png` (425K, 1900×2100) | 0 0 1824 1936 |
| `sequence-weekly-run.html` (32.1K) | `sequence-weekly-run.svg` (29.6K) | `sequence-weekly-run.png` (1.0M, 1900×2100) | 0 0 1840 1984 |
| `sequence-doctor.html` (25.1K) | `sequence-doctor.svg` (23.1K) | `sequence-doctor.png` (748K, 1900×1500) | 0 0 1840 1424 |
| `sequence-commit-draft.html` (22.0K) | `sequence-commit-draft.svg` (19.7K) | `sequence-commit-draft.png` (536K, 1900×1100) | 0 0 1840 1000 |

Le PNG est pratique pour README/issues/slides ; le SVG reste la version vectorielle éditable/print HQ ; le HTML garde le header, la légende et le thème interactif.

## Régénération

```sh
# 1. Extraire le <svg> de chaque HTML -> *.svg (+ header XML)
python3 << 'PY'
import re, pathlib
for p in pathlib.Path("doc/diagrams").glob("*.html"):
    html = p.read_text(encoding="utf-8")
    m = re.search(r'<svg[\s\S]*?</svg>', html)
    if not m: continue
    svg = m.group(0)
    if not svg.lstrip().startswith('<?xml'):
        svg = '<?xml version="1.0" encoding="UTF-8"?>\n' + svg
    p.with_suffix('.svg').write_text(svg, encoding='utf-8')
    print(f"{p.name} -> {p.with_suffix('.svg').name}")
PY

# 2. Rasteriser chaque SVG -> *.png via Chrome headless (pas de dépendance npm)
google-chrome --headless --disable-gpu --no-sandbox --window-size=1600,2600 --screenshot=$(pwd)/doc/diagrams/architecture.png file://$(pwd)/doc/diagrams/architecture.svg
google-chrome --headless --disable-gpu --no-sandbox --window-size=1900,2100 --screenshot=$(pwd)/doc/diagrams/classes.png file://$(pwd)/doc/diagrams/classes.svg
google-chrome --headless --disable-gpu --no-sandbox --window-size=1900,1100 --screenshot=$(pwd)/doc/diagrams/sequence-commit-draft.png file://$(pwd)/doc/diagrams/sequence-commit-draft.svg
google-chrome --headless --disable-gpu --no-sandbox --window-size=1900,1500 --screenshot=$(pwd)/doc/diagrams/sequence-doctor.png file://$(pwd)/doc/diagrams/sequence-doctor.svg
google-chrome --headless --disable-gpu --no-sandbox --window-size=1900,2100 --screenshot=$(pwd)/doc/diagrams/sequence-weekly-run.png file://$(pwd)/doc/diagrams/sequence-weekly-run.svg
```

Taille de fenêtre = viewBox + ~80-160 px de marge (SVG rendu à l'échelle 1). `google-chrome` est disponible sur les runners CI (Ubuntu `google-chrome-stable`) ; à défaut, `rsvg-convert` ou `inkscape` produisent un rendu équivalent.
