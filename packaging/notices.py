"""
Write THIRD-PARTY-NOTICES.txt for the Windows build from the licence files the
bundled packages actually ship — run inside the build venv (build.ps1 does).

Covers Python itself, Tcl/Tk (tkinter's runtime), PyInstaller's bootloader,
and every distribution reachable from the app's direct dependencies. Generated
rather than hand-written so it can't drift from what is really in the bundle.
"""
import re
import sys
from importlib import metadata
from pathlib import Path

# What the app imports directly; everything they require is walked from here.
ROOTS = ("Flask", "requests", "Pillow", "centrifuge-python", "pystray")
# Build tools: bundled only as PyInstaller's bootloader, listed separately.
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
              "packaging", "pywin32-ctypes", "pip", "setuptools"}
LICENSE_NAME = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)
RULE = "=" * 78


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def closure():
    seen, todo = {}, list(ROOTS)
    while todo:
        name = todo.pop()
        key = _norm(name)
        if key in seen or key in BUILD_ONLY:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for req in dist.requires or ():
            if "extra ==" in req:
                continue
            marker = req.split(";", 1)[1] if ";" in req else ""
            if "sys_platform" in marker and "win32" not in marker \
                    and "!=" not in marker:
                continue
            todo.append(re.split(r"[\s<>=!~;\[(]", req.strip(), 1)[0])
    return sorted(seen.values(), key=lambda d: _norm(d.metadata["Name"]))


def licence_texts(dist):
    texts = []
    for f in dist.files or ():
        if LICENSE_NAME.search(str(f).replace("\\", "/")):
            try:
                texts.append((str(f), Path(dist.locate_file(f)).read_text(
                    encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return texts


def section(title, licence, texts):
    out = [RULE, title, f"Licence: {licence}" if licence else "", RULE]
    for name, text in texts:
        out += [f"--- {name} ---", text.strip(), ""]
    if not texts:
        out.append("(no licence file shipped; see the licence named above)\n")
    return "\n".join(line for line in out if line is not None)


def main(out_path):
    parts = ["Album Art Overlay bundles the following third-party software.",
             "Each is used under the licence reproduced below.", ""]

    base = Path(sys.base_prefix)
    py = base / "LICENSE.txt"
    parts.append(section(f"Python {sys.version.split()[0]}", "PSF License",
                         [(py.name, py.read_text(encoding="utf-8", errors="replace"))]
                         if py.is_file() else []))
    tcl = [p for p in (base / "tcl").glob("*/license.terms")]
    parts.append(section("Tcl/Tk (tkinter)", "Tcl/Tk License (BSD-style)",
                         [(str(p.relative_to(base)), p.read_text(errors="replace"))
                          for p in sorted(tcl)[:2]]))

    for dist in closure():
        meta = dist.metadata
        licence = meta.get("License-Expression") or meta.get("License") or ""
        if len(licence) > 80:  # some put the whole text here; it's below anyway
            licence = licence.splitlines()[0][:80]
        if not licence:
            licence = "; ".join(c.split("::")[-1].strip() for c in
                                meta.get_all("Classifier") or () if c.startswith("License"))
        parts.append(section(f"{meta['Name']} {dist.version}", licence,
                             licence_texts(dist)))

    pyi = metadata.distribution("pyinstaller")
    parts.append(section(
        f"PyInstaller {pyi.version} (bootloader only)",
        "GPL-2.0-or-later with the PyInstaller bootloader exception, which "
        "permits distributing the bootloader with programs under any licence",
        licence_texts(pyi)))

    Path(out_path).write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "THIRD-PARTY-NOTICES.txt")
