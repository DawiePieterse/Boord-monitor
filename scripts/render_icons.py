#!/usr/bin/env python3
"""Render Boord Monitor's Home Screen icons (frontend/icons/icon-{512,192,180}.png).

The artwork is Boord's crate-and-leaf mark, drawn by Boord's own
scripts/render_icons.py. This script borrows its render() rather than copying
the geometry, so a change to the mark in Boord reaches Monitor the next time
this is run, and only the leaf colour is Monitor's:

    python3 scripts/render_icons.py                 # Boord checked out at ../Boord
    python3 scripts/render_icons.py --boord /path/to/Boord

Needs Pillow (pip install pillow, in any scratch venv - the server does not).

Monitor used to ship a byte-for-byte copy of Labour's icon, so on a phone
with both installed the two were the same violet leaf and you had to read the
label to tell them apart. Every sibling's leaf colour is listed in Boord's
render_icons.py; teal is the one picked here. Boord's notes there warn that
teal sits near Notes' sky, but measured in Lab it is about as far from sky
(and from Field's green, its nearest) as Labour's violet is from its own
nearest neighbour, and the remaining hues (orange, pink, lime) each sit just
as close to Admin's yellow, Packhouse's red or Field's green. If Boord's
script grows a --monitor option, use that and delete this one.
"""

import argparse
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MONITOR_LEAF = (20, 184, 166, 255)  # teal #14b8a6


def load_boord_renderer(boord_dir):
    path = os.path.join(boord_dir, "scripts", "render_icons.py")
    if not os.path.isfile(path):
        raise SystemExit("Boord's scripts/render_icons.py not found under %s - pass --boord" % boord_dir)
    # Borrowing another repo's file should leave no trace in it.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("boord_render_icons", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boord", metavar="DIR", default=os.path.join(os.path.dirname(ROOT), "Boord"),
                    help="Boord's checkout (default: ../Boord)")
    ap.add_argument("--out", metavar="DIR", default=os.path.join(ROOT, "frontend", "icons"),
                    help="where to write the PNGs (default: frontend/icons)")
    args = ap.parse_args()
    boord = load_boord_renderer(args.boord)
    boord.write(boord.render(MONITOR_LEAF), args.out)


if __name__ == "__main__":
    main()
