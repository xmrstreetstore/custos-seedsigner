# Bundled Javacard applets

`.cap` files offered by **DIY Tools → Install Applet**.

These are **third-party binaries that get flashed onto a card which will hold
keys.** Treat them accordingly: verify on import, verify on update, and do not add
one without recording where it came from.

## What is here

| File | Source | SHA256 |
|---|---|---|
| `SeedKeeper-0.2-official.cap` | Satochip's official release [Seedkeeper-Applet v0.2-0.1](https://github.com/Toporin/Seedkeeper-Applet/releases/tag/v0.2-0.1) (`SeedKeeper-v0.2-0.1.cap`), imported via [3rdIteration/seedsigner](https://github.com/3rdIteration/seedsigner) `javacard-cap/` | `28dbae3c7c130a6f7d0e6d05f41386ffd93976fd290eaa5d8db708b9903dabcd` |

Checked against the upstream `javacard-cap.sha256` manifest when imported, and it
matched. Re-checked on 2026-10-24 against the file attached to Satochip's own GitHub
release: byte-identical. `javacard-cap.sha256` here is the upstream manifest reduced to what we ship.

### Deliberately not shipped

The NDEF variant (`SeedKeeper-Ndef-v0.2-0.1.cap` in the same release) is not
bundled. It adds an NDEF tag for phone apps; Custos talks to the card over the
PN532 as a plain contactless smartcard and does not need it. If you want it on a
card, put the `.cap` on the microSD (see below) or restore it with
`git checkout fbc4de51 -- javacard-cap/SeedKeeper-Ndef-v0.2-0.1.cap` (and add its hash to the manifest). Its SHA256 is
`ef776360415ee0c64881b1e36339ffba815231aab2406014559d18fdaa632c9b`.

## Licence and source

The SeedKeeper applet is Satochip's work and is licensed **AGPL-3.0**, separately
from this repository's MIT licence. The `.cap` here is the unmodified binary from
the release above. Its complete corresponding source is the
[`v0.2-0.1` tag of Toporin/Seedkeeper-Applet](https://github.com/Toporin/Seedkeeper-Applet/tree/v0.2-0.1),
and the licence text is in that repository's `LICENSE` file.

```bash
cd javacard-cap && sha256sum -c javacard-cap.sha256
```

## Why the repo root

`ToolsDIYInstallAppletView` looks for applets in
`Path(__file__).resolve().parents[3] / "javacard-cap"`. The app lives at
`/opt/src/seedsigner/…` in the image, so that resolves to **`/opt/javacard-cap/`**.

`seedsigner-os`'s `build.sh` clones this repo into `rootfs-overlay/opt/`, so keeping
the directory at the repo root puts it exactly where the view expects it, with no
change to the OS build. `build.sh`'s trim list does not touch it.

The installer also reads `<microsd>/javacard-cap/`, so an applet can be supplied on
the card without rebuilding an image. Files present in both places are listed with
`(Internal)` / `(MicroSD)` prefixes.

## Adding another applet

Drop the `.cap` in, add its hash to `javacard-cap.sha256`, and record its provenance
in the table above. It will appear in the install menu automatically. Only add
applets you are prepared to vouch for — SeedKeeper is the only one this fork exists
to support.
