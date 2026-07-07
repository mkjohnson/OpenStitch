# embdmaker

Convert SVG artwork and common image files into Brother-compatible embroidery
files.

The default output is `.pes`, the common Brother embroidery format. This is a
small digitizer: it turns SVG strokes into running stitches, closed filled SVG
shapes into hatch-fill stitches, and JPG/PNG/PDF artwork into quantized
scanline-fill stitches. For best results, start with clean, high-contrast art at
the physical size you want to stitch.

## Install

```powershell
pip install -r requirements.txt
```

## Usage

```powershell
python svg2brother.py design.svg -o design.pes
```

Useful options:

```powershell
python svg2brother.py design.svg -o design.pes --fit-width-mm 90
python svg2brother.py design.svg -o design.pes --fill-spacing-mm 0.45 --max-stitch-mm 3
python svg2brother.py design.svg -o design.dst --format dst
```

## Application

Start the local application:

```powershell
python app.py
```

Then open `http://127.0.0.1:8765/`.

The app provides:

- Import for SVG, PES, JPG, PNG, and PDF files.
- Animated stitch preview with zoom, pan, playback, and stitch stepping.
- PES download after conversion.
- Color-block toggles and filtered PES recreation.
- A hamburger menu for moving between Convert, Library, and saved previews.
- A Library page with live preview, generated previews, and PES files.
- A Thread Inventory page for saved thread colors, close-match suggestions, and
  estimated thread usage by design color.

## PES Viewer CLI

Generate a standalone HTML preview of a `.pes` file:

```powershell
python pes_viewer.py design.pes -o design.html
```

You can also import SVG, JPG, PNG, or PDF files directly into the viewer. This
converts the artwork into stitches, writes the animated HTML preview, and saves a
Brother `.pes` beside it:

```powershell
python pes_viewer.py design.svg -o design.html --fit-width-mm 90
python pes_viewer.py image.png -o image.html --fit-width-mm 90 --max-colors 6
python pes_viewer.py art.pdf -o art.html --fit-width-mm 90 --pdf-page 1
```

To choose where the saved Brother file goes:

```powershell
python pes_viewer.py design.svg -o design.html --pes-output design.pes
```

For the browser import workflow, start the local application:

```powershell
python app.py
```

Then open `http://127.0.0.1:8765/`, choose an SVG, PES, JPG, PNG, or PDF file,
and click `Convert and View`. SVG/image/PDF uploads create both an animated
viewer and a downloadable Brother `.pes` file. For JPG, PNG, and PDF files, use
`Color flattening` to merge similar shades before stitches are created. Increase
it when near-identical shades become separate thread colors, or set it to `0` to
preserve the quantized palette.

Open `Thread Inventory` to add thread colors you own, including brand, thread
name or number, hex color, and quantity. New generated viewers estimate thread
use per color and show the closest inventory match, along with colors that look
like they need to be purchased.

In the server-generated viewer, each color block has a checkbox. Uncheck any
blocks you do not want, then click `Recreate PES` to save a new Brother file
containing only the selected color blocks.

Use the hamburger menu in the application or generated viewer pages to return to
the converter, open the library, or download the current PES file when one is
available. The Library page previews generated designs inline, so you can browse
previous conversions without opening each one in a separate tab.

Open the generated HTML file in a browser to inspect stitch paths, jump stitches,
trim markers, color changes, design size, and thread colors. The viewer includes
a stitch slider, play/pause animation, playback speed control, zoom controls,
drag-to-pan, mouse-wheel zoom, and toggles for jumps, needle points, trims, and
color changes. SVG-generated previews include a download button for the saved
`.pes` file.

The toolpath view is canvas-rendered so larger designs do not create one browser
DOM element per stitch. Needle-point dots are automatically skipped while zoomed
out on very large designs, then shown again when you zoom in.

## Notes

- Filled closed shapes are stitched with horizontal hatch rows.
- Open paths and stroked shapes are stitched as running stitches.
- SVG colors are converted into thread color stops.
- JPG, PNG, and PDF artwork is quantized into a limited color palette before
  stitching. Use `--max-colors` to control how many thread colors are generated,
  and `--color-merge-distance` to flatten similar shades.
- PDF conversion uses the selected page, rasterized before stitching.
- SVG coordinates are read using the standard 96 DPI SVG unit convention.
- Complex digitizing details such as satin columns, underlay, pull compensation,
  applique stops, and trim commands are outside this first version.
