# Custos OS — app

**An air-gapped bitcoin signer that keeps your seed on a PIN-protected SeedKeeper
smartcard, and remembers nothing once it powers off.**

This is the application half of [Custos OS](https://custos-os.com): a fork of
[SeedSigner](https://github.com/SeedSigner/seedsigner) that adds SeedKeeper
smartcard support, by contact (SEC1210 HAT) or NFC (PN532). Everything outside the
smartcard code is stock SeedSigner, on purpose. The OS half lives in
[custos-seedsigner-os](https://github.com/xmrstreetstore/custos-seedsigner-os), and
release images, build scripts and verifiers are in
[custos-os](https://github.com/xmrstreetstore/custos-os).

> **Status: v0.1, beta.** Tested on a Raspberry Pi Zero v1.3 with the SEC1210 HAT
> and a PN532 reader. Use it with a seed you also hold a backup of.

## What it adds to SeedSigner

| Feature | Where |
|---|---|
| **Load a seed from a card.** 12 or 24 words go straight into memory. If the card also holds a passphrase, you choose whether to use it. | Seeds › Load a seed › From SeedKeeper |
| **Save a seed to a card,** labelled with its fingerprint. The write is read back and checked. A BIP-39 passphrase is written with the seed, and the confirm screen says so. | Seed › Backup › Save to SeedKeeper |
| **Contact or NFC.** A card-wait screen accepts either reader; a tapped card gets a live connection bar and the flow continues once the link is steady. | every card operation |
| **Full-keyboard PIN,** 4–16 characters, masked on screen. | every card operation |
| **Wrong-PIN countdown.** Each wrong PIN shows the attempts left; a card set up on Custos wipes itself after the fifth. | every card operation |
| **Card setup.** Install or uninstall the SeedKeeper applet from a file bundled in the image, and set a new card's PIN. Uninstall only offers Satochip-family applets, so a card's own packages cannot be removed by mistake. | Tools › Smartcard |
| **Reader selection.** Switch the SEC1210 or PN532 off. | Settings › Hardware › Smartcard interfaces |

Smaller changes: gold accent colour (`#E8B33A`), a smartcard icon font
(`tools/make_custos_icons.py`), the Custos boot logo, Hardware moved to the main
Settings menu, and the 320×240 panel as the default display.

## Where the code is

| Path | What |
|---|---|
| `src/seedsigner/helpers/seedkeeper.py` | Card connection, PIN, secrets, applet install/uninstall, `CardWatch` |
| `src/seedsigner/views/seedkeeper_views.py` | The smartcard views |
| `src/seedsigner/gui/screens/seedkeeper_screens.py` | PIN entry and the card-wait screen |
| `javacard-cap/` | The bundled SeedKeeper applet — see its [README](javacard-cap/README.md) |

The smartcard code was ported from
[3rdIteration's SeedSigner fork](https://github.com/3rdIteration/seedsigner)
(`dev` branch), taking only the SeedKeeper features rather than adopting the fork.

## Building

The app is built into an image by
[custos-seedsigner-os](https://github.com/xmrstreetstore/custos-seedsigner-os)
from the `seedkeeper-support` branch. See
[custos-os](https://github.com/xmrstreetstore/custos-os) for the build and
verification scripts. The stock SeedSigner README, including its developer and
test instructions, is kept as [README.seedsigner.md](README.seedsigner.md).

## Licence and credits

MIT, like SeedSigner — see [LICENSE.md](LICENSE.md). Built on SeedSigner by the
SeedSigner contributors. Smartcard support ported from 3rdIteration. SeedKeeper is
by [Satochip](https://satochip.io); the bundled applet is AGPL-3.0, and its source
is linked in [javacard-cap/README.md](javacard-cap/README.md).

Custos OS is an independent project, not affiliated with SeedSigner or Satochip.
