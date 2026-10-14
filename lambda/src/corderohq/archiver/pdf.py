"""Convert receipt images and PDFs to PDF/A format for archival."""

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

_GS_BINARY = os.environ.get("GS_BINARY", "/var/task/bin/gs")
_GS_LIBRARY_PATH = os.environ.get("GS_LIBRARY_PATH", "/var/task/lib")
_GS_RESOURCE_PATH = os.environ.get("GS_RESOURCE_PATH", "/var/task/share/Resource/Init:/var/task/share/lib")

_CONTENT_TYPE_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _gs_env() -> dict[str, str]:
    """Return the environment for a Ghostscript subprocess.

    The Ghostscript libraries must stay out of the Lambda process environment. The
    bundle directory holds a `libcrypto.so.3` from the Ghostscript build image. A
    process-wide `LD_LIBRARY_PATH` makes the Python `ssl` module load that library in
    place of the runtime library. The two OpenSSL versions differ, so the import of
    `ssl` fails, and the Lambda then fails to import its handler module.
    """
    return {**os.environ, "LD_LIBRARY_PATH": _GS_LIBRARY_PATH, "GS_LIB": _GS_RESOURCE_PATH}


def convert_to_pdfa(data: bytes, content_type: str) -> bytes:
    """Convert an image or a PDF to the PDF/A-2b format with Ghostscript.

    Images are first converted to PDF via Pillow, then Ghostscript produces PDF/A.
    PDFs go directly through Ghostscript.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        if content_type == "application/pdf":
            input_pdf = tmp / "input.pdf"
            input_pdf.write_bytes(data)
        else:
            input_pdf = tmp / "intermediate.pdf"
            suffix = _CONTENT_TYPE_SUFFIX.get(content_type, ".bin")
            image_path = tmp / f"input{suffix}"
            image_path.write_bytes(data)
            img = Image.open(image_path)
            img.save(str(input_pdf), "PDF", resolution=300.0)

        output_pdf = tmp / "output.pdf"

        result = subprocess.run(
            [
                _GS_BINARY,
                "-dPDFA=2",
                "-dBATCH",
                "-dNOPAUSE",
                "-sColorConversionStrategy=UseDeviceIndependentColor",
                "-sDEVICE=pdfwrite",
                f"-sOutputFile={output_pdf}",
                str(input_pdf),
            ],
            capture_output=True,
            env=_gs_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Ghostscript failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout.decode(errors='replace')}\n"
                f"stderr: {result.stderr.decode(errors='replace')}"
            )

        return output_pdf.read_bytes()


def get_page_count(pdf_data: bytes) -> int:
    """Return the number of pages in a PDF. Ghostscript counts them."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        input_pdf = tmp / "input.pdf"
        input_pdf.write_bytes(pdf_data)

        result = subprocess.run(
            [
                _GS_BINARY,
                "-q",
                "-dNODISPLAY",
                "-dNOSAFER",
                f"--permit-file-read={tmp_dir}/",
                "-c",
                f"({input_pdf}) (r) file runpdfbegin pdfpagecount = quit",
            ],
            capture_output=True,
            env=_gs_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Ghostscript page count failed (exit {result.returncode}):\n"
                f"stderr: {result.stderr.decode(errors='replace')}"
            )

        return int(result.stdout.decode().strip())


def extract_page(pdf_data: bytes, page_num: int) -> bytes:
    """Take one page out of a PDF. Ghostscript does the work."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        input_pdf = tmp / "input.pdf"
        input_pdf.write_bytes(pdf_data)
        output_pdf = tmp / "output.pdf"

        result = subprocess.run(
            [
                _GS_BINARY,
                "-dNOPAUSE",
                "-dBATCH",
                f"-dFirstPage={page_num}",
                f"-dLastPage={page_num}",
                "-sDEVICE=pdfwrite",
                f"-sOutputFile={output_pdf}",
                str(input_pdf),
            ],
            capture_output=True,
            env=_gs_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Ghostscript page extraction failed (exit {result.returncode}):\n"
                f"stderr: {result.stderr.decode(errors='replace')}"
            )

        return output_pdf.read_bytes()
