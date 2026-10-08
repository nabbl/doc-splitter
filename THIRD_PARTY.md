# Third-party software and model notices

License review is based on the pinned model repository, installed distribution
metadata and bundled notices, not a claim of legal advice. Do not remove those
notices when redistributing the container or downloaded snapshot.

## Model versus examples

`nutrientdocs/doc-split-v1` at
`dccadc69dd4ae69c63f42aa6715f249c6e5e6cf8` declares **Apache-2.0** in its model card.
The pinned repository has no separate LICENSE file; preserve its downloaded
README/model provenance and the [Apache-2.0 terms](https://www.apache.org/licenses/LICENSE-2.0).
This implementation uses its documented tensor interfaces, normalization, CRF
parameters and calibration. Attribution: Nutrient / nutrientdocs, doc-split-v1.

Separately inspected `nutrientdocs/doc-split-demo` at
`803671733cfa1c1f6591145528e03866242bd5fd`: repository metadata/file listing exposed
no license declaration or LICENSE file. **No demo source, fixtures or UI were
copied.** Model Apache-2.0 metadata is not assumed to license that separate demo.
The test documents here are generated synthetic fixtures.

## Runtime dependencies

| Component | Pinned version | License / redistribution notes |
| --- | --- | --- |
| ONNX Runtime | 1.23.2 | MIT; retain packaged third-party notices |
| NumPy | 2.2.6 | BSD-3-Clause; wheel includes bundled numerical-library notices |
| pikepdf | 10.0.2 | MPL-2.0; unchanged library, retain notices/source availability obligations |
| pypdfium2 | 4.30.0 | Apache-2.0 OR BSD-3-Clause plus PDFium third-party licenses |
| Pillow | 12.0.0 | MIT-CMU; retain bundled codec notices |
| Hugging Face tokenizers | 0.22.1 | Apache-2.0 |
| Tesseract OCR | Debian 5.3.0-2 | Apache-2.0; installed Debian copyright notices include dependencies |
| German/English tessdata | Debian 1:4.1.0-2 | Apache-2.0 |
| Python base image | 3.12.12, digest-pinned | PSF/Python terms plus Debian package licenses |
| uv | 0.9.17, digest-pinned | MIT OR Apache-2.0 |

The Python transitive closure is version/hash-locked in `uv.lock`: anyio, filelock,
coloredlogs, Deprecated, h11, humanfriendly, PyYAML use MIT; click, fsspec, httpcore,
httpx, idna, lxml, protobuf use BSD-3-Clause; mpmath/sympy use BSD; wrapt uses BSD-2-Clause;
flatbuffers, hf-xet, huggingface-hub use Apache-2.0; packaging uses Apache-2.0 OR
BSD-2-Clause; certifi uses MPL-2.0; tqdm uses MPL-2.0 AND MIT; typing-extensions uses
PSF-2.0. Preserve the actual wheel `*.dist-info/licenses` and PDFium license bundle:
short metadata is not a replacement for the full bundled terms.

Development-only pytest/pluggy/iniconfig/ruff use MIT, Pygments uses BSD-2-Clause.
No AGPL PDF library or commercial v2 model was introduced. Python dependencies are
not modified; any future modification/redistribution of MPL-covered files needs
the corresponding source/notice handling. Base image and Debian transitive packages
retain their `/usr/share/doc/*/copyright` files; image SBOM records the actual build.
