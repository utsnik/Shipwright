# Ship of Harkinian 9.2.3 for Wii U

An unofficial Wii U port of [Ship of Harkinian](https://github.com/HarbourMasters/Shipwright)
(SoH), the PC port of The Legend of Zelda: Ocarina of Time.

This release contains **no game data**. You need your own Ocarina of Time ROM, turned into an
`oot.o2r` file with the desktop (PC) version of SoH. The Wii U app cannot do that step itself.

## What is in the zip

- `soh.wuhb`: the app.
- `soh.o2r`: SoH's own assets (fonts, UI). This file is not game data.
- `README.md`: this file.

## Requirements

- A Wii U running the [Aroma](https://aroma.foryour.cafe/) homebrew environment.
- An SD card with at least 100 MB free (plus the size of any texture pack).
- A PC (Windows, macOS or Linux) to make `oot.o2r` once.
- An Ocarina of Time ROM that SoH supports. Tested on Wii U: **NTSC-U 1.2** (N64). Other ROMs
  on SoH's supported list should work but are untested on the Wii U.

## 1. Make `oot.o2r` on your PC

1. Download SoH **9.2.3** for your PC from the
   [Shipwright releases page](https://github.com/HarbourMasters/Shipwright/releases). The version
   must match: an `oot.o2r` made by another SoH version will not load.
2. Start it and point it at your ROM when it asks. It creates `oot.o2r` in its data folder.
3. You can then close the PC version; only the `oot.o2r` file is needed.

## 2. Copy the files to the SD card

```
sd:/wiiu/apps/soh.wuhb
sd:/wiiu/apps/soh923/soh.o2r
sd:/wiiu/apps/soh923/oot.o2r
```

The folder must be called `soh923`. The app creates it on first start if it is missing, and keeps
its settings (`shipofharkinian.json`) and saves there.

## 3. Play

Start **Ship of Harkinian** from the Wii U Menu.

## Texture packs (optional)

SoH texture packs are `.o2r` or `.otr` files; put them in `sd:/wiiu/apps/soh923/mods/`. Packs are
made and hosted by their authors, never bundled with this port. Most are on
GameBanana, for example [Djipi's 3DS Experience](https://gamebanana.com/mods/477979). Packs with 512 px textures have been
tested on the Wii U. Very large packs use more of the console's memory and can make loading slower.

## Known limits

- Runs at about 20 fps (the original game's rate). Heavy scenes can dip.
- Change settings in the in-game menu. Do not edit `shipofharkinian.json` while the game is
  running: it is rewritten on exit.
- Close the game with HOME -> Close before taking the SD card out.

## Credits

Ship of Harkinian by HarbourMasters and contributors; libultraship and Fast3D by their authors.
This port is not affiliated with or endorsed by Nintendo or HarbourMasters.
