# WASM Signature: Why and How

## Background

The FoxESS Cloud has two APIs:

1. **Open API** (`open.foxesscloud.com`) — documented, authenticated with an API key and a simple MD5 signature (see [API_DEVIATIONS.md](../API_DEVIATIONS.md) for the gotchas). This is what the integration uses for schedule management and polled sensor data.

2. **Web portal API** (`www.foxesscloud.com`) — undocumented, used by the foxesscloud.com web app. This is needed for the real-time WebSocket that streams inverter data every ~5 seconds during smart sessions.

The web portal API requires a `signature` header on every request. The algorithm that produces this signature is **not documented** and is **obfuscated in the portal's JavaScript**.

## Why not just reimplement it in Python?

The Open API's signature is a straightforward MD5 hash that can be computed in one line of Python. The web portal signature is different: the algorithm is compiled into a WebAssembly module that the portal's JavaScript calls at runtime. Reverse-engineering the exact algorithm from obfuscated WASM bytecode and maintaining a Python equivalent would be fragile — any time FoxESS updates their portal, the reimplementation could silently break.

Alternatives considered:

| Approach | Problem |
|---|---|
| Pure Python reimplementation | Fragile — algorithm is obfuscated, breaks silently on portal updates |
| Headless browser | Too heavy for Home Assistant (runs on Raspberry Pis and similar) |
| Avoid the web portal API entirely | Loses real-time WebSocket data; forced to rely on 5-minute polling |

## What the WASM module is

The file `signature.wasm` is the **original signature algorithm extracted directly from foxesscloud.com**. It was compiled with [Emscripten](https://emscripten.org/) (a C/C++ to WebAssembly toolchain), which is a standard approach for deploying native code in browsers.

Rather than decompiling and reimplementing this, we run the WASM module as-is using [wasmtime](https://wasmtime.dev/), a lightweight WebAssembly runtime with Python bindings. This gives us:

- **Exact compatibility** — the same code that runs in the browser runs here
- **No maintenance burden** — if FoxESS updates the algorithm, we update the `.wasm` file
- **Sandboxed execution** — WASM runs in a memory-safe sandbox with no filesystem or network access

## How it works

```
foxesscloud.com JavaScript
        |
        v
signature.wasm  (Emscripten-compiled, ~16 KB)
        |
        v
wasmtime (Python WASM runtime)
        |
        v
signature.py wrapper
        |
        v
web_session.py._make_headers()
        |
        v
HTTP request with { "signature": "02ed6973...5245784" }
```

The Python wrapper (`foxess/signature.py`) does three things:

1. **Loads the WASM once** as a process-level singleton via `wasmtime`
2. **Bridges Python strings into WASM memory** — writes UTF-8 bytes into the WASM linear memory, calls the exported `begin_signature` function, and reads the result back
3. **Manages the WASM stack** — saves and restores the stack pointer around each call to prevent memory leaks

Because the WASM module was compiled with Emscripten, the wrapper also provides three Emscripten runtime functions that the module expects to exist:

- `emscripten_memcpy_big` — large memory copy
- `emscripten_resize_heap` — heap growth (stubbed to no-op; the default heap is sufficient)
- `setTempRet0` — temporary return register (stubbed)

## When is the signature used?

Only when **web credentials** are configured (see [README: Web credentials](../README.md#web-credentials-optional)). The signature is required to:

1. Log in to the web portal and obtain a session token
2. Authenticate the WebSocket upgrade request for real-time data

If web credentials are not configured, the WASM module is never loaded and `wasmtime` is never imported. The integration falls back to the Open API with standard polling.
