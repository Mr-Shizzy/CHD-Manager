# CHD Manager

A friendly Windows app for turning disc images (`.cue`/`.bin`, `.gdi`, `.iso`) into compressed **CHD** files and back again. It works out whether each disc is a **CD or a DVD** for you, so there is no "which button do I press" guessing, and it picks sensible settings for your PC.

CHD Manager is a front end for `chdman`, the tool from the [MAME](https://github.com/mamedev/mame) project. It is portable: one folder, nothing installed, nothing written outside it.

![CHD Manager, dark theme](docs/screenshot-dark.png)

<details>
<summary>Light theme</summary>

![CHD Manager, light theme](docs/screenshot-light.png)

</details>

## Features

- **Queue** of files or whole folders (searched recursively). Loose `.bin` files that already belong to a `.cue` are skipped.
- **CD or DVD, detected automatically.** `.cue`/`.gdi`/`.toc` are CD. An `.iso` is CD if it fits on a CD, DVD if it is larger, and PSP images are DVD. It also looks inside the image to label the platform (PS1, PS2, PSP, Dreamcast, Saturn, Sega CD). Right-click any item to force CD or DVD.
- **Convert and extract.** `.chd` files are read to see whether they hold a CD, GD-ROM or DVD, then extracted to `.cue`+`.bin`, `.gdi`, or `.iso`. Or just verify them.
- **Suggested settings.** It looks at your PC and your queue and fills in thread count, compression, and so on, with the reason for each. Weak PCs automatically get low-priority mode so Windows stays usable.
- **Safety.** Optional verify after creating each CHD. Optional "send sources to the Recycle Bin", and only after the new CHD passed verification, so your source files are never permanently deleted.
- **Options.** Compression presets or custom codecs, hunk size, threads, skip/overwrite/rename existing files, output folder (with folder structure kept), per-track `.bin` extraction.
- **Progress and log** for every job, an Info window, a summary of the space you saved, and a dark and a light theme.
- **chdman updates.** One click checks MAME's latest release and, if you confirm, installs it (checksum-verified, with a backup of the old one).

## Download and use

1. Get the zip from the [Releases](../../releases) page and unzip it anywhere.
2. Run **CHD Manager.exe**.
3. Click **Add Files** or **Add Folder**, check the **Suggested** tab, press **Start**.

Windows 10/11, 64-bit. Python is not needed. Settings are saved in `chd_manager_settings.json` next to the exe.

**About warnings:** the exe is not code-signed, so Windows SmartScreen may say "unknown publisher" (choose *More info → Run anyway*) and some antivirus programs are suspicious of this kind of packaged Python program. If you would rather not run a binary, [run it from source](#run-from-source) or [build it yourself](#build-the-exe).

## How CD vs DVD is chosen

| Image | Type |
| --- | --- |
| `.cue`, `.gdi`, `.toc`, loose `.bin` | CD (`createcd`) |
| `.iso` up to 737,280,000 bytes (an 80-minute CD) | CD |
| `.iso` larger than that | DVD (`createdvd`) |
| PSP images | DVD |

If a PS2 game on a CD slightly exceeds the size limit, or anything else is detected wrongly, right-click it and force the type.

## Network use

CHD Manager is offline except for two things, both about chdman:

- At startup it asks GitHub for the number of the latest MAME release (you can turn this off in the **Output** tab). It never downloads anything by itself.
- When you press the update button and confirm, it downloads the official release from `github.com/mamedev/mame`. Opening that download needs [7-Zip](https://7-zip.org). If it is missing, the app offers to install it with `winget`, and if you decline only the update button stops working.

## Run from source

Needs Python 3 with tkinter (tested with 3.12) and `chdman.exe` from a [MAME release](https://github.com/mamedev/mame/releases) placed next to the script:

```
python CHD_Manager.pyw
```

## Build the exe

Needs Python 3 (tested with 3.12) and 7-Zip.

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

This installs PyInstaller, downloads the latest official chdman (checking its SHA-256), builds `CHD Manager.exe`, and writes the portable folder and a zip to `dist/`. Use `-ChdmanPath` to supply your own chdman instead.

## Status

Tested on Windows 11. The conversion paths (CD from `.cue`/`.bin`, lone `.bin`, ISO as CD and DVD, extraction back to identical files, verification, corrupted-file detection) were checked against the real chdman using synthetic discs. It has not been tried on large real collections on many machines, so please open an issue if something misbehaves, and keep your originals until you have checked a converted game boots.

## Credits and license

- `chdman` is from the [MAME](https://github.com/mamedev/mame) project and its authors. CHD Manager is not made by, affiliated with, or endorsed by MAME. "MAME" is a registered trademark of Gregory Ember.
- Built with help from Claude (Anthropic); designed and tested by Mr-Shizzy.
- This project includes no games, BIOS files, or other copyrighted game content. Use it only with discs you own.

CHD Manager's own code is under the [0BSD license](LICENSE): do whatever you like with it, no strings attached. Forks and remakes are welcome. The release bundles chdman, Python and Tcl/Tk, whose licenses are in [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
