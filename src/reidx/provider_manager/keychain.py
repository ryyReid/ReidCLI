from __future__ import annotations

import base64
import getpass
import hashlib
import os
import socket
import sys
from pathlib import Path

from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.provider_manager.keychain")

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def _dpapi_encrypt(plaintext: bytes) -> bytes:
        blob_in = _DATA_BLOB()
        blob_in.pbData = ctypes.cast(
            ctypes.create_string_buffer(plaintext, len(plaintext)),
            ctypes.POINTER(ctypes.c_char),
        )
        blob_in.cbData = len(plaintext)
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise OSError("DPAPI CryptProtectData failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    def _dpapi_decrypt(ciphertext: bytes) -> bytes:
        blob_in = _DATA_BLOB()
        blob_in.pbData = ctypes.cast(
            ctypes.create_string_buffer(ciphertext, len(ciphertext)),
            ctypes.POINTER(ctypes.c_char),
        )
        blob_in.cbData = len(ciphertext)
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise OSError("DPAPI CryptUnprotectData failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _machine_key() -> bytes:
    parts = [
        getpass.getuser(),
        socket.gethostname(),
        str(Path.home()),
        sys.platform,
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", raw, b"reidx-keychain-v1", 100000, dklen=32)


def _xor_cipher(data: bytes, key: bytes) -> bytes:
    return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    raw = plaintext.encode("utf-8")
    if _IS_WINDOWS:
        encrypted = _dpapi_encrypt(raw)
    else:
        encrypted = _xor_cipher(raw, _machine_key())
    return base64.b64encode(encrypted).decode("ascii")


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    raw = base64.b64decode(ciphertext)
    if _IS_WINDOWS:
        decrypted = _dpapi_decrypt(raw)
    else:
        decrypted = _xor_cipher(raw, _machine_key())
    return decrypted.decode("utf-8")


def secure_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
