# Fixtures the functional suite serves rather than fetches

## media-upload-probe.png

The image `shopware-media-upload` is pointed at. 64x64 RGBA, a checkerboard in
Shopware blue on white.

It is here because the check used to name a URL on somebody else's host, and
that is two failure modes wearing one hat:

- `assets.shopware.com` answered **403**, so every run reported the tool broken
  when the fixture was.
- `upload.wikimedia.org` then answered **"Cannot open source stream"** — the file
  had gone (404) — and failed the whole static job with 47 of 48 checks passing.

Neither says anything about `shopware-media-upload`. A check that can only pass
while a third party keeps a file where it was is not measuring the tool.

So the lane serves it: `.github/actions/setup-lane` copies this file into the
shop's `public/`, and the workflow points `MCP_MEDIA_UPLOAD_URL` at the shop's
own URL. The tool fetches the URL server-side, and a server can always reach its
own `public/` — same question, nothing in the way that we do not control.

**Provenance.** Generated for this repository — a plain geometric pattern, no
third-party rights, nothing to expire. To regenerate it, or to make one at a
different size:

```python
import pathlib, struct, zlib

W = H = 64
CELL = 8
BLUE, WHITE = (24, 154, 219, 255), (255, 255, 255, 255)

rows = bytearray()
for y in range(H):
    rows.append(0)  # PNG filter type 0 (None), one per scanline
    for x in range(W):
        rows.extend(BLUE if ((x // CELL) + (y // CELL)) % 2 == 0 else WHITE)


def chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


pathlib.Path("media-upload-probe.png").write_bytes(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    + chunk(b"IEND", b"")
)
```

## Running the media-upload check locally

The copy step only runs in CI, so a local run has nothing to fetch. Either put
the file where your shop serves it:

```bash
cp functional/assets/media-upload-probe.png <your-shopware>/public/
```

…or point the check somewhere else with `MCP_MEDIA_UPLOAD_URL`. Without either,
the check **SKIPs** with the reason — an image the lane does not serve is
missing setup, not evidence that the tool is broken.
