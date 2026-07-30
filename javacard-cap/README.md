# Bundled Javacard applets

`.cap` files offered by **DIY Tools → Install Applet**.

These are **third-party binaries that get flashed onto a card which will hold
keys.** Treat them accordingly: verify on import, verify on update, and do not add
one without recording where it came from.

## What is here

| File | Source | SHA256 |
|---|---|---|
| `SeedKeeper-0.2-official.cap` | [3rdIteration/seedsigner](https://github.com/3rdIteration/seedsigner) `javacard-cap/`, from Satochip's official release | `28dbae3c7c130a6f7d0e6d05f41386ffd93976fd290eaa5d8db708b9903dabcd` |

Checked against the upstream `javacard-cap.sha256` manifest when imported, and it
matched. `javacard-cap.sha256` here is the upstream manifest reduced to what we ship.

### Deliberately not shipped

The NDEF variant was bundled at first and then dropped, because everything it adds
is for contactless use and this device has no NFC: the SEC1210 is a contact reader
on `/dev/ttyAMA0`. Offering it in the install menu meant offering a second,
near-identically named applet that nothing here can talk to over the air.

If the PN532 is ever fitted, restore it with `git show fbc4de51 --` and put its row
back. Its provenance was the same upstream directory, NDEF variant, SHA256
`ef776360415ee0c64881b1e36339ffba815231aab2406014559d18fdaa632c9b`. Nothing else
needs changing: the install menu lists whatever `.cap` files it finds.

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
