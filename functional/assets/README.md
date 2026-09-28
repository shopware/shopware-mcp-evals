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

So the check fetches this file, from one of two places we control:

- **By default, from this repository:**
  `https://raw.githubusercontent.com/shopware/shopware-mcp-evals/main/functional/assets/media-upload-probe.png`.
  It needs no setup and it is a public URL, which matters: an `APP_ENV=prod` shop
  validates upload URLs (`shopware.media.enable_url_validation`) and refuses a
  localhost one, so copying the file into a prod lane's `public/` does not work.
- **In CI, from the shop itself:** `.github/actions/setup-lane` copies this file
  into the shop's `public/`, and the workflow points `MCP_MEDIA_UPLOAD_URL` at the
  shop's own URL. The lane runs `dev`, where that validation is off, and the run
  needs no network beyond the lane.

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

Nothing to set up: the default URL is this repository's copy, and the shop only
needs outbound internet to fetch it. An offline lane can serve the file itself
and say so with `MCP_MEDIA_UPLOAD_URL` — on a `dev` lane only, see above.

The check does **not** skip when the image is unreachable. It FAILS, naming the
URL and not calling the tool: the image is committed, so an unreachable one is
this suite's problem, never a finding about `shopware-media-upload`, and a skip
is exactly how the trunk lane went without this check unnoticed. Opt out
explicitly with `--skip-media-upload`.
