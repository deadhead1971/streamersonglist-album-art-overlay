"""
The native file dialog behind every Browse… button.

Tkinter must own the main thread of its interpreter, which Flask request
threads are not, so the dashboard runs this in a short-lived child process
(dashboard.api_browse) as ``python -m app.filedialog``. It used to be a
``python -c`` string.

The chosen path goes back through a file (``--out``), not stdout: the
installed app runs under pythonw, which has no stdout at all, and a pipe is
decoded with the locale's code page, which can't carry every character a
Windows path can.
"""

import argparse
import sys
from pathlib import Path


def _parse(argv):
    parser = argparse.ArgumentParser(prog="filedialog")
    parser.add_argument("--kind", choices=("open", "save"), default="open")
    parser.add_argument("--initialfile", default="")
    parser.add_argument("--initialdir", default="")
    # "Label:*.png *.jpg;Label2:*.*", as the page's data-filetypes carries it.
    parser.add_argument("--filetypes", default="")
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)

    import tkinter
    import tkinter.filedialog as fd

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    types = [tuple(t.split(":", 1)) for t in args.filetypes.split(";") if ":" in t]
    opts = {"parent": root, "initialfile": args.initialfile,
            "filetypes": types or [("All files", "*.*")]}
    if args.initialdir and Path(args.initialdir).is_dir():
        opts["initialdir"] = args.initialdir
    if args.kind == "save":
        path = fd.asksaveasfilename(defaultextension=".png", **opts)
    else:
        path = fd.askopenfilename(**opts)
    root.destroy()

    # Cancel is an empty file, not a missing one: a missing file means the
    # dialog never got as far as answering.
    Path(args.out).write_text(path or "", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
