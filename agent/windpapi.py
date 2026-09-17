"""Windows DPAPI wrappers (CryptProtectData / CryptUnprotectData) via ctypes.

On Windows, ``protect`` seals bytes so they can only be unsealed by the same
Windows user account on the same machine — so a copied ``secret.key`` is useless
to anyone else, even with full read access to the file. On every other platform
(and if DPAPI is somehow unavailable) these are identity passthroughs, so the
caller's logic is identical everywhere.

Dependency-free: uses only ctypes from the standard library.
"""
from __future__ import annotations

import sys

available = False

if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        _crypt32 = ctypes.windll.crypt32
        _kernel32 = ctypes.windll.kernel32

        def _in_blob(data: bytes) -> _BLOB:
            buf = ctypes.create_string_buffer(bytes(data), len(data))
            return _BLOB(len(data),
                         ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

        def _out_bytes(blob: _BLOB) -> bytes:
            try:
                return ctypes.string_at(blob.pbData, int(blob.cbData))
            finally:
                if blob.pbData:
                    _kernel32.LocalFree(blob.pbData)

        def protect(data: bytes) -> bytes:
            din, dout = _in_blob(data), _BLOB()
            if not _crypt32.CryptProtectData(ctypes.byref(din), None, None,
                                             None, None, 0, ctypes.byref(dout)):
                raise OSError("CryptProtectData failed")
            return _out_bytes(dout)

        def unprotect(blob: bytes) -> bytes:
            din, dout = _in_blob(blob), _BLOB()
            if not _crypt32.CryptUnprotectData(ctypes.byref(din), None, None,
                                               None, None, 0, ctypes.byref(dout)):
                raise OSError("CryptUnprotectData failed")
            return _out_bytes(dout)

        available = True
    except Exception:
        available = False

if not available:
    def protect(data: bytes) -> bytes:    # noqa: E302  (identity passthrough)
        return bytes(data)

    def unprotect(blob: bytes) -> bytes:  # noqa: E302
        return bytes(blob)
