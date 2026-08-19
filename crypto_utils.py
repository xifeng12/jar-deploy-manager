import base64
import ctypes
import os
from ctypes import wintypes

from cryptography.fernet import Fernet, InvalidToken


class SecretEncryptionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def is_windows():
    return os.name == "nt"


def encrypt(plaintext):
    if not plaintext:
        return ""
    if is_windows():
        return "dpapi:" + base64.b64encode(_protect(plaintext.encode("utf-8"))).decode("ascii")
    return "fernet:" + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext):
    if not ciphertext:
        return ""
    if ciphertext.startswith("dpapi:"):
        if not is_windows():
            raise SecretEncryptionError("DPAPI secrets can only be decrypted on Windows")
        return _unprotect(base64.b64decode(ciphertext.removeprefix("dpapi:"))).decode("utf-8")
    if ciphertext.startswith("fernet:"):
        try:
            return _fernet().decrypt(ciphertext.removeprefix("fernet:").encode("ascii")).decode("utf-8")
        except InvalidToken as error:
            raise SecretEncryptionError("secret cannot be decrypted") from error
    if is_windows():
        try:
            return _unprotect(base64.b64decode(ciphertext)).decode("utf-8")
        except (OSError, ValueError):
            pass
    raise SecretEncryptionError("secret is not encrypted")


def is_encrypted(value):
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("dpapi:", "fernet:")):
        return True
    if not is_windows():
        return False
    try:
        _unprotect(base64.b64decode(value))
    except (OSError, ValueError):
        return False
    return True


def _fernet():
    key = os.environ.get("DEPLOY_CONFIG_KEY", "")
    if not key:
        raise SecretEncryptionError("DEPLOY_CONFIG_KEY is required outside Windows")
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as error:
        raise SecretEncryptionError("DEPLOY_CONFIG_KEY is invalid") from error


def _blob(value):
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(value):
    input_blob, input_buffer = _blob(value)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    if not crypt32.CryptProtectData(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _unprotect(value):
    input_blob, input_buffer = _blob(value)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    if not crypt32.CryptUnprotectData(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
