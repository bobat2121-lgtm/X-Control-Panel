"""Pixel-art icons for the panel: browser-tab / bookmark icons, one per page, plus the app icon.

Each icon is a 16x16 map drawn by hand (one letter per pixel). Run this file to rebuild the PNGs:
    python panel/icons/make_icons.py
Outputs <name>.png (64x64, crisp nearest-neighbour upscale), <name>-16/32.png, <name>.ico and preview.png.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent

PALETTE = {
    ".": None,
    "K": "#1a1410",  # ink outline
    "R": "#b5432a",  # vermilion / rust
    "r": "#7a2a1c",  # dark rust
    "T": "#e8b868",  # tan
    "t": "#f6dca6",  # parchment
    "G": "#e6b422",  # gold
    "g": "#a8801a",  # dark gold
    "S": "#8c8792",  # stone
    "s": "#4a4652",  # dark stone
    "W": "#f4ecd8",  # white plaster
    "P": "#f4b6c2",  # blossom
    "p": "#e07a95",  # blossom shade
    "N": "#231c3d",  # night
    "F": "#f7b33b",  # flame
    "f": "#ffe08a",  # flame core
    "E": "#6f8f3a",  # leaf green
    "B": "#6b4a3a",  # wood
    "b": "#3b2418",  # dark wood
    "M": "#f3e6b0",  # moon
}

ICONS = {
    # Monitor · 物見 (lookout): a paper lantern that stays lit all night
    "monitor": [
        ".......KK.......",
        "......K..K......",
        "....KKKKKKKK....",
        "...KbbbbbbbbK...",
        "..KRRRRRRRRRRK..",
        ".KRRFFFFFFFFRRK.",
        ".KRFFffffffFFRK.",
        ".KrrrrrrrrrrrrK.",
        ".KRFffffffffFRK.",
        ".KRFFffffffFFRK.",
        ".KrrrrrrrrrrrrK.",
        "..KRRFFFFFFRRK..",
        "...KbbbbbbbbK...",
        "....KKKKKKKK....",
        ".......KK.......",
        "......KRRK......",
    ],
    # Feed · 巻物 (scroll): your posts, written out, sealed and ready
    "feed": [
        "................",
        ".KK..........KK.",
        "KGGKKKKKKKKKKGGK",
        "KBBKttttttttKBBK",
        "KBBKtKKKKKttKBBK",
        "KBBKttttttttKBBK",
        "KBBKtKKKKKKtKBBK",
        "KBBKttttttttKBBK",
        "KBBKtKKKKttKKBBK",
        "KBBKttttttttKBBK",
        "KBBKtKKKtRRtKBBK",
        "KBBKttttRRRtKBBK",
        "KGGKKKKKKKKKKGGK",
        ".KK..........KK.",
        "................",
        "................",
    ],
    # Build Lab · 鍛冶場 (smithy): a blade glowing on the anvil
    "build_lab": [
        "....f......F....",
        "..F....f.....f..",
        "......F....F....",
        "................",
        ".KKKKKKKKKKKKKK.",
        "KFFfffffffFFKGbK",
        ".KKKKKKKKKKKKKK.",
        "KKKKKKKKKKKKKKK.",
        "KSSSSSSSSSSSSSK.",
        ".KKsSSSSSSSSsKK.",
        "....KsSSSSsK....",
        "....KsSSSSsK....",
        "...KsSSSSSSsK...",
        "..KbbbbbbbbbbK..",
        "..KBBBBBBBBBBK..",
        "..KKKKKKKKKKKK..",
    ],
    # Scoreboard · 算盤 (abacus): three rods, counted beads (vermilion) rising left to right
    "scoreboard": [
        "KKKKKKKKKKKKKKKK",
        "KBBBBBBBBBBBBBBK",
        "KB.TTT.TTT.K..BK",
        "KB..K...K..K..BK",
        "KB..K...K.RRR.BK",
        "KbbbbbbbbbbbbbbK",
        "KB.RRR.RRR.RRRBK",
        "KB..K...K...K.BK",
        "KB..K..RRR.RRRBK",
        "KB..K...K...K.BK",
        "KB.TTT..K..RRRBK",
        "KB..K...K...K.BK",
        "KB.TTT.TTT..K.BK",
        "KB..K...K...K.BK",
        "KBBBBBBBBBBBBBBK",
        "KKKKKKKKKKKKKKKK",
    ],
    # Control Room · 本陣 (headquarters): the commander's kabuto
    "control_room": [
        ".G............G.",
        ".GG..........GG.",
        "..GG........GG..",
        "...GG..KK..GG...",
        "....GGKRRKGG....",
        "....KKRRRRKK....",
        "...KrRRRRRRrK...",
        "..KrRRRRRRRRrK..",
        "..KRRGRRRRGRRK..",
        ".KKKKKKKKKKKKKK.",
        "KRRKbbbbbbbbKRRK",
        "KRRKK.KKKK.KKRRK",
        ".KRRKKK..KKKRRK.",
        ".KrRRRRRRRRRRrK.",
        ".KrrrrrrrrrrrrK.",
        "..KKKKKKKKKKKK..",
    ],
    # App · the floating castle under the moon
    "app": [
        "............MM..",
        "...........M....",
        "......G..G.M....",
        "......KKKK..MM..",
        ".....KssssK.....",
        "....KsKKKKsK....",
        ".....WWKKWW.....",
        "...KKKKKKKKKK...",
        "..KssssssssssK..",
        "....WKWWWWKW....",
        "..SSSSSSSSSSSS..",
        ".EEEEEEEEEEEEEE.",
        "..BBBBBBBBBBBB..",
        "...bBBBBBBBBb...",
        ".....bBBBBb.....",
        ".......bb.......",
    ],
}


def render(rows: list[str]) -> Image.Image:
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        assert len(row) == 16, (y, row)
        for x, ch in enumerate(row):
            col = PALETTE[ch]
            if col:
                img.putpixel((x, y), (int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16), 255))
    return img


def build() -> None:
    sheet = Image.new("RGBA", (len(ICONS) * 144 + 16, 160), (35, 28, 61, 255))
    for i, (name, rows) in enumerate(ICONS.items()):
        assert len(rows) == 16, name
        base = render(rows)
        for size in (16, 32):
            base.resize((size, size), Image.NEAREST).save(HERE / f"{name}-{size}.png")
        base.resize((64, 64), Image.NEAREST).save(HERE / f"{name}.png")
        base.resize((48, 48), Image.NEAREST).save(HERE / f"{name}.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        sheet.alpha_composite(base.resize((128, 128), Image.NEAREST), (16 + i * 144, 16))
    sheet.save(HERE / "preview.png")


if __name__ == "__main__":
    build()
    print("icons:", ", ".join(ICONS))
