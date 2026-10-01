# Ship of Harkinian 9.2.3 for Wii U

An unofficial Wii U port of [Ship of Harkinian](https://github.com/HarbourMasters/Shipwright)
(SoH), the PC port of The Legend of Zelda: Ocarina of Time.

This release contains **no game data**. You supply your own Ocarina of Time ROM and turn it into
an `oot.o2r` file once, on a PC, with the desktop version of SoH. The Wii U cannot do that step
itself: SoH's converter only exists in the PC builds (the Switch port works the same way).

## What is in the zip

| File | What it is |
|---|---|
| `soh.wuhb` | The app you start from the Wii U Menu. |
| `soh.o2r` | SoH's own assets (fonts, menus). Not game data. |
| `soh.rpx` | The bare program. Only needed to make your own artwork (see the last section). |
| `README.md` | This guide. |
| `fix_blank_skyboxes.py` | Optional: repairs a white screen in two places with Djipi's texture pack (see below). |

## What you need

- A Wii U running the [Aroma](https://aroma.foryour.cafe/) homebrew environment.
- An SD card with about 100 MB free, plus room for any texture pack.
- A Windows, Linux or macOS computer, used once.
- A legally dumped Ocarina of Time ROM that SoH supports. Check yours at
  [ship.equipment](https://ship.equipment/), or compare its SHA-1 with SoH's
  [list of supported hashes](https://github.com/HarbourMasters/Shipwright/blob/develop/docs/supportedHashes.json).
  Any supported version works; you need only one.

Tested on a Wii U so far: the **NTSC-U 1.2** (US N64) ROM. The other supported versions go
through the same code and should work, but have not been tried on a Wii U yet.

## Step 1: make `oot.o2r` on your computer

1. Download SoH **9.2.3** for your computer from the
   [Shipwright releases page](https://github.com/HarbourMasters/Shipwright/releases).
   **The version must be 9.2.3.** A file made by any other version is rejected with an
   "Outdated ROM Archives" message.
2. Run it once with your ROM:
   - **Windows:** extract the zip, start `soh.exe` and select your ROM when asked.
   - **Linux:** put the ROM in the same folder as `soh.appimage`, then run it
     (`chmod +x soh.appimage` first if needed).
   - **macOS:** start `soh.app` and select your ROM when asked.
3. Wait until the game's title screen appears, then close it. The new file is:
   - Windows and Linux: `oot.o2r` in the same folder as `soh.exe` / `soh.appimage`.
   - macOS: `~/Library/Application Support/com.shipofharkinian.soh/oot.o2r`.

If you gave it a Master Quest ROM you get `oot-mq.o2r` instead (or as well). Copy it next to
`oot.o2r` in step 2. Master Quest has not been tested on a Wii U yet.

## Step 2: copy the files to the SD card

```
sd:/wiiu/apps/soh.wuhb
sd:/wiiu/apps/soh923/soh.o2r
sd:/wiiu/apps/soh923/oot.o2r
```

The data folder must be called exactly `soh923`, whatever you name the `.wuhb`. The app also keeps
its settings (`shipofharkinian.json`) and save files there.

## Step 3: play

Put the card back, start the Wii U and open **Ship of Harkinian** from the Wii U Menu.

## Texture packs (optional)

Texture packs are `.o2r` or `.otr` files. Put them in:

```
sd:/wiiu/apps/soh923/mods/
```

Packs are made and hosted by their authors and are never bundled with this port. Most are on
GameBanana, for example [Djipi's 3DS Experience](https://gamebanana.com/mods/477979). Djipi's pack
has been played on the Wii U exactly as downloaded (1024 px textures), with plenty of memory to
spare in the areas tested. Big packs make scene loading slower.

### White screen in the Kokiri Shop with Djipi's pack

Djipi's pack replaces five "look around" backgrounds with fully transparent pictures: the Kokiri
Shop (shown while you talk to the shopkeeper) and the Carpenters' Tent. The Wii U draws them white.
`fix_blank_skyboxes.py` rebuilds those five pictures from **your own** `oot.o2r` and writes a small
pack that goes after Djipi's in `mods/` (packs load in alphabetical order, and the last one wins):

```
pip install pillow
python3 fix_blank_skyboxes.py oot.o2r "Djipi's 3DE - 01 Main Textures.o2r" zz-fix-blank-skyboxes.o2r
```

Copy `zz-fix-blank-skyboxes.o2r` to `sd:/wiiu/apps/soh923/mods/`. Delete it to undo.

## Troubleshooting

- **"Outdated ROM Archives"**: your `oot.o2r` was made by a different SoH version. Make it again
  with SoH 9.2.3 (step 1).
- **"No O2R files found. Generate one now?"**: the app cannot find `oot.o2r`. Choose **No**
  (the Wii U cannot generate it), then check that `soh.o2r` and `oot.o2r` are both in
  `sd:/wiiu/apps/soh923/`, spelled exactly like that.
- **The screen freezes and the console stops responding**: hold the power button to turn it off,
  then start it again. Please report what you were doing when it happened.
- **Settings did not stick**: change them in the in-game menu. Don't edit `shipofharkinian.json`
  while the game is running, because the game rewrites it when it closes.
- Always leave with **HOME -> Close** before taking the SD card out.

## Known limits

- Runs at about 20 fps, the original game's rate; busy scenes can dip below that.
- Distant textures can shimmer (no mipmapping yet).
- Entering a new area can pause briefly while it loads from the SD card, more so with big
  texture packs.
- Some torch and lamp glows in Hyrule Field show through the hills as small yellow dots.

## Source code

This port is two branches on top of SoH 9.2.3:
[utsnik/Shipwright `wiiu-release`](https://github.com/utsnik/Shipwright/tree/wiiu-release)
and the GX2 (Wii U graphics) backend in
[utsnik/libultraship `wiiu-release`](https://github.com/utsnik/libultraship/tree/wiiu-release).

## Make your own artwork

The Wii U Menu icon and the two boot screens (TV and GamePad) are stored inside `soh.wuhb`. To
use your own, build a new `.wuhb` from `soh.rpx` and your pictures. You don't need to compile
anything.

### 1. Make the pictures

| Picture | Size in pixels | Shown |
|---|---|---|
| Icon | 128 x 128 | In the Wii U Menu |
| TV boot screen | 1280 x 720 | On the TV while the game starts |
| GamePad boot screen | 854 x 480 | On the GamePad while the game starts |

Save them as PNG files at exactly these sizes. Any image editor can do this; scale or crop the
picture to the size, don't just change the canvas.

### 2. Install `wuhbtool`

`wuhbtool` comes with devkitPro, the free Wii U homebrew toolkit:

- **Windows:** run the [devkitPro installer](https://github.com/devkitPro/installer/releases) and
  include the Wii U development tools.
- **Linux and macOS:** set up [devkitPro pacman](https://devkitpro.org/wiki/devkitPro_pacman),
  then run `sudo dkp-pacman -S wut-tools`.

The tool is then at `/opt/devkitpro/tools/bin/wuhbtool` (on Windows,
`C:\devkitPro\tools\bin\wuhbtool.exe`).

### 3. Build your `.wuhb`

Put `soh.rpx` and your three pictures in one folder, open a terminal there and run (one line):

```
wuhbtool soh.rpx soh.wuhb --name="Ship of Harkinian" --short-name="SoH" --author="HarbourMasters" --icon=icon.png --tv-image=tv.png --drc-image=gamepad.png
```

`--name` is the title shown in the Wii U Menu, so you can change it too. Copy the new `soh.wuhb`
to `sd:/wiiu/apps/`, replacing the old one. Your `soh923` folder, settings and saves stay as
they are.

**Sharing:** for your own console, use any picture you like. If you share your `.wuhb` with
others, don't put Nintendo's logos or box art in it. The artwork in this release is made only
from SoH's own ship icon for that reason.

## Credits

Ship of Harkinian by HarbourMasters and contributors; libultraship and Fast3D by their authors.
This port is not affiliated with or endorsed by Nintendo or HarbourMasters.
