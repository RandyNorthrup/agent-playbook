"""Build playbook.html from app/playbook.src.html and the icon set. Run after editing the page source:

  python playbook/app/build_page.py            write playbook.html
  python playbook/app/build_page.py --check    exit 1 if playbook.html is not what the source builds
  python playbook/app/build_page.py --help     print this and exit

The output is the same bytes on every OS: UTF-8, LF line endings. Exit codes: 0 done (or up to date), 1 stale,
2 the command line was wrong."""
import pathlib
import sys

APP = pathlib.Path(__file__).resolve().parent
PB = APP.parent
HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>body{margin:0}</style></head><body>')


def build():
    """The page as bytes."""
    src = (APP / "playbook.src.html").read_bytes().decode("utf-8").replace("\r\n", "\n")
    icons = (APP / "icons.json").read_bytes().decode("utf-8").replace("\r\n", "\n").strip()
    return (HEAD + src.replace("__ICONS__", icons) + "</body></html>\n").encode("utf-8")


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        sys.exit(0)
    unknown = [a for a in argv if a != "--check"]
    if unknown:
        print("build_page.py: unknown argument %s\n\n%s" % (" ".join(unknown), __doc__), file=sys.stderr)
        sys.exit(2)
    out = PB / "playbook.html"
    data = build()
    if "--check" in argv:
        try:
            same = out.read_bytes().replace(b"\r\n", b"\n") == data
        except OSError:
            same = False
        print("playbook.html is up to date" if same else "playbook.html is stale: run python app/build_page.py")
        sys.exit(0 if same else 1)
    with open(str(out), "wb") as f:
        f.write(data)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1:])
