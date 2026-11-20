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

## McpEvalsFixture

A throwaway app for `functional/app_capabilities.py`, behind `--app-fixture`.
Its `Resources/mcp.xml` declares one capability of each kind:

- the tool `echo`, which returns the `text` it was called with
- the prompt `briefing`, one user message
- the resource `mcp-evals-fixture://status`, an `application/json` text

The files are templates.
`functional/app_fixture.py` fills in `{{BASE_URL}}` and `{{SECRET}}`, zips them, and serves the app from the runner process on a free port.
Both secrets are minted per run, and the server answers only requests whose signature verifies.

The check installs the app through the Admin API, runs the checks, and uninstalls and removes it in a `finally`.
It must not stay installed: the LLM eval fetches every prompt the server lists.
If the cleanup check fails, remove the app by hand before running the LLM eval.

### What the shop has to allow

The app server speaks plain HTTP on a private address, which the app system refuses by default.
Lift exactly those two guards for exactly the address the shop reaches the runner by, and nothing else:

```yaml
shopware:
    app_system:
        allow_unencrypted_traffic: true
        allowed_private_ip_addresses: ['127.0.0.1']
```

In CI, `.github/actions/setup-lane` writes this as `config/packages/zzz-mcp-evals-app-system.yaml` before the cache is cleared.
The runner and the shop share the machine there, so the defaults of `--app-fixture-host` and `--app-fixture-bind` (`127.0.0.1`) are right.

### Running it against a containerised lane

The shop in a container reaches the host by another address, for example `192.168.127.254` on Podman on macOS.
Put that address into `allowed_private_ip_addresses`, clear the cache, and tell the runner to advertise it:

```bash
python -m functional.runner --endpoint admin --app-fixture --app-fixture-host 192.168.127.254 --app-fixture-bind 0.0.0.0
```

`--app-fixture-bind 0.0.0.0` is only needed when the container cannot reach the host's loopback.
Shopware also refuses to install an app while its `APP_URL` cannot be reached from inside the container (HTTP 503 "APP_URL ... could not be reached").
A lane whose `APP_URL` names a port only the host serves needs a forwarder for the duration of the run.
Remove the config file and clear the cache again afterwards.

### Before the fix lands

On a shop without https://github.com/shopware/shopware/pull/22623, `prompts/get` answers `-32603 Error while handling prompt`, and `resources/read` returns the app's whole JSON answer as the resource text.
Both are entries in `functional/known_failures.py`: they run, are reported as expected, and do not fail the run.
Once the pinned Shopware carries the fix, the reporter warns that the entries passed and should be deleted.
