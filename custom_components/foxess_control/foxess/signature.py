"""FoxESS Cloud request signature generator.

The FoxESS web portal requires a ``signature`` header on every request.
The algorithm is implemented in a WebAssembly module (``signature.wasm``)
shipped alongside this file.  This wrapper loads the WASM once and
exposes a simple ``generate_signature(url, token, lang, timestamp)``
function for use by :mod:`.web_session`.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

import wasmtime

_LOGGER = logging.getLogger(__name__)

_WASM_PATH = Path(__file__).with_name("signature.wasm")


class _SignatureEngine:
    """Thin wrapper around the FoxESS signature WASM module."""

    def __init__(self) -> None:
        self._store = wasmtime.Store()
        module = wasmtime.Module.from_file(self._store.engine, str(_WASM_PATH))

        linker = wasmtime.Linker(self._store.engine)
        self._memory: wasmtime.Memory | None = None

        def _memcpy_big(caller: wasmtime.Caller, dest: int, src: int, num: int) -> int:
            assert self._memory is not None
            data = self._memory.data_ptr(caller)
            length = self._memory.data_len(caller)
            buf = (ctypes.c_ubyte * length).from_address(
                ctypes.addressof(data.contents)
            )
            tmp = bytes(buf[src : src + num])
            for i, b in enumerate(tmp):
                buf[dest + i] = b
            return dest

        linker.define_func(
            "env",
            "emscripten_memcpy_big",
            wasmtime.FuncType([wasmtime.ValType.i32()] * 3, [wasmtime.ValType.i32()]),
            _memcpy_big,
        )
        linker.define_func(
            "env",
            "emscripten_resize_heap",
            wasmtime.FuncType([wasmtime.ValType.i32()], [wasmtime.ValType.i32()]),
            lambda _caller, _size: 0,
        )
        linker.define_func(
            "env",
            "setTempRet0",
            wasmtime.FuncType([wasmtime.ValType.i32()], []),
            lambda _caller, _val: None,
        )

        instance = linker.instantiate(self._store, module)
        exports = instance.exports(self._store)
        self._memory = exports["memory"]
        self._begin_sig = exports["begin_signature"]
        self._stack_save = exports["stackSave"]
        self._stack_restore = exports["stackRestore"]
        self._stack_alloc = exports["stackAlloc"]

        # Run WASM constructors
        exports["__wasm_call_ctors"](self._store)

    def _write_string(self, s: str) -> int:
        encoded = s.encode("utf-8") + b"\x00"
        ptr: int = self._stack_alloc(self._store, len(encoded))
        assert self._memory is not None
        data = self._memory.data_ptr(self._store)
        length = self._memory.data_len(self._store)
        buf = (ctypes.c_ubyte * length).from_address(ctypes.addressof(data.contents))
        for i, b in enumerate(encoded):
            buf[ptr + i] = b
        return ptr

    def _read_string(self, ptr: int) -> str:
        assert self._memory is not None
        data = self._memory.data_ptr(self._store)
        length = self._memory.data_len(self._store)
        buf = (ctypes.c_ubyte * length).from_address(ctypes.addressof(data.contents))
        chars: list[str] = []
        while buf[ptr] != 0:
            chars.append(chr(buf[ptr]))
            ptr += 1
        return "".join(chars)

    def generate(self, url: str, token: str, lang: str, timestamp: str) -> str:
        """Return the signature string for the given request parameters."""
        sp: int = self._stack_save(self._store)
        try:
            result_ptr = self._begin_sig(
                self._store,
                self._write_string(url),
                self._write_string(token),
                self._write_string(lang),
                self._write_string(timestamp),
            )
            return self._read_string(result_ptr)
        finally:
            self._stack_restore(self._store, sp)


# Module-level singleton — loaded once per process.
_engine: _SignatureEngine | None = None


def generate_signature(url: str, token: str, lang: str, timestamp: str) -> str:
    """Generate the ``signature`` header value for a FoxESS web API request.

    Parameters match the JS call: ``getSignature(url, token, lang, timestamp)``.
    """
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = _SignatureEngine()
    return _engine.generate(url, token, lang, timestamp)
