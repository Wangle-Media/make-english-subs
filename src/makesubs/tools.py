"""Locating GPU libraries installed from pip."""
from __future__ import annotations

import ctypes
import glob
import os
import sys
from functools import cache
from pathlib import Path


@cache
def load_nvidia_libs() -> None:
    """Make pip-installed CUDA libraries (nvidia-cublas/cudnn) visible to CTranslate2.

    Must run before faster_whisper/ctranslate2 is imported.
    """
    try:
        import nvidia  # namespace package from nvidia-*-cu12 wheels
    except ImportError:
        return
    roots = [Path(p) for p in nvidia.__path__]
    if sys.platform == "win32":
        for root in roots:
            for d in root.glob("*/bin"):
                os.add_dll_directory(str(d))
                os.environ["PATH"] = f"{d}{os.pathsep}{os.environ.get('PATH', '')}"
    elif sys.platform.startswith("linux"):
        # Preload with RTLD_GLOBAL so CTranslate2 finds them without LD_LIBRARY_PATH.
        for pattern in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*",
                        "cudnn/lib/libcudnn*.so.*"):
            for root in roots:
                for lib in sorted(glob.glob(str(root / pattern))):
                    try:
                        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass
