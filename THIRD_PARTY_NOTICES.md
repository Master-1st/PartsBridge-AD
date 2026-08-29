# Third-party notices

PartsBridge AD is distributed under the GNU Affero General Public License v3.0 or later. See `LICENSE`. The matching application source and build specification are provided in the repository and the source ZIP beside each Windows release.

Source installations use the following direct dependencies. Windows releases bundle them and their required runtime dependencies:

- `easyeda2kicad 1.0.1` — GNU Affero General Public License v3.0; source: <https://github.com/uPesy/easyeda2kicad.py>
- `altium-monkey 2026.8.21` — GNU Affero General Public License v3.0 or later; source: <https://github.com/wavenumber-eng/altium_monkey>

## Windows v0.3.8 runtime dependencies

| Component | Version | License / upstream source |
| --- | --- | --- |
| wn-geometer | 2026.8.21 | MIT; [source](https://github.com/wavenumber-eng/geometer); includes OCCT under LGPL-2.1 with the Open CASCADE exception, Clipper2 under Boost-1.0, and RapidJSON notices |
| freetype-py | 2.5.1 | BSD-3-Clause; [source](https://github.com/rougier/freetype-py) |
| jsonschema-rs | 0.48.5 | MIT; [matching source tag](https://github.com/Stranger6667/jsonschema/tree/python-v0.48.5) |
| lxml | 6.0.2 | BSD-3-Clause and included third-party notices; [source](https://github.com/lxml/lxml) |
| lz4 | 4.4.5 | BSD-3-Clause; [source](https://github.com/python-lz4/python-lz4) |
| msgspec | 0.21.1 | BSD-3-Clause; [source](https://github.com/jcrist/msgspec) |
| Pillow | 12.3.0 | MIT-CMU and included notices; [source](https://github.com/python-pillow/Pillow) |
| uharfbuzz | 0.56.0 | Apache-2.0; [source](https://github.com/harfbuzz/uharfbuzz) |
| CPython | 3.12.10 | PSF and included third-party notices; [matching source](https://github.com/python/cpython/tree/v3.12.10) |
| Tcl / Tk | 8.6.15 | Tcl/Tk license terms; [Tcl source](https://github.com/tcltk/tcl/tree/core-8-6-15), [Tk source](https://github.com/tcltk/tk/tree/core-8-6-15) |
| PyInstaller bootloader | 6.22.2 | GPL-2.0-or-later with the PyInstaller bootloader exception; [source](https://github.com/pyinstaller/pyinstaller) |

The build retains Python dependency metadata and available license files in `_internal/*.dist-info`. Geometer's full notices, including the OCCT exception, are in `_internal/geometer/licenses`. CPython's license is in `_internal/licenses/Python`; Tk's license is in `_internal/_tk_data/license.terms`.

Additional exact-version notices for jsonschema-rs, FreeType 2.13.2, HarfBuzz 14.3.0, Tcl 8.6.15, libxml2 2.11.9 and libxslt 1.1.39 are in `third_party_licenses/` in the source tree and `_internal/licenses/third-party/` in the Windows build. The source locations for these notices are recorded in that directory's README. Portions of this software use the FreeType Project (https://www.freetype.org); the FreeType credit and license are retained there.

Unmodified Python dependency releases can also be obtained by package name and version from [PyPI](https://pypi.org/). Preserve the corresponding license texts, notices and access to source when redistributing. These notices do not replace the individual upstream license terms.

## Component data and trademarks

No user's BOM, Altium component libraries, downloaded STEP models, account credentials or caches are distributed in this repository or its application release. Data fetched separately from LCSC/EasyEDA remains subject to the applicable source terms and must be independently checked before engineering use.

LCSC/JLCPCB, EasyEDA, and Altium are trademarks of their respective owners. This project is an independent interoperability tool and is not endorsed by those vendors.
