Put your own Wii keys in this folder. CHD Manager doesn't contain them and never downloads them.

A file here can be called anything ending in .txt, .key, .bin or .hex. It holds the key as 32 hexadecimal
characters (16 bytes), for example one line:

    common: <32 hex characters>
    korean: <32 hex characters>   (only for Korean discs)

A line with no label is taken as the common key. A file of exactly 16 raw bytes also works (a name containing
"korea" makes it the Korean key). You can instead paste the key into Settings, Formats.

The key is only needed to write RVZ, WIA or WBFS from a Wii disc. Reading every format, and everything for GameCube and
Wii U discs, works without it. If Dolphin is on this PC, its DolphinTool.exe can do the RVZ and WIA conversions instead.
