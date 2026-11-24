"""Draw a command's real output as an SVG picture of a terminal window, with no browser.

Every picture in this repository comes from a real run: make_examples.py runs the demo, saves its closing table
to examples/closing-table.txt, and draws docs/img/demo.svg from that file with this tool. It draws exactly the
lines it is given. It adds nothing and removes nothing except trailing spaces; lines longer than --width are cut
and end in an ellipsis, and what it prints says how many lines were cut. A line is drawn red when it holds a word
such as "refused", "rejected" or "failed", and green when it holds one such as "PASS" or "approved".

Standard library only. It writes one file.

    python -I tools/term_svg.py --in examples/closing-table.txt --out docs/img/demo.svg --title "python -I demo.py"
    python -I tools/term_svg.py --run "python -I demo.py --list" --out <picture.svg>
"""
import argparse
import html
import pathlib
import re
import subprocess
import sys

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CHAR_W, LINE_H, PAD, BAR = 8.4, 19, 16, 34


def draw(lines, title, width):
    cut = 0
    shown = []
    for line in lines:
        line = ANSI.sub("", line.rstrip().replace("\t", "    "))
        if len(line) > width:
            line, cut = line[: width - 1] + chr(8230), cut + 1
        shown.append(line)
    w = int(PAD * 2 + CHAR_W * width)
    h = int(BAR + PAD * 2 + LINE_H * max(len(shown), 1))
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
        'aria-label="%s">' % (w, h, w, h, html.escape("Terminal output: " + title, quote=True)),
        '<rect width="%d" height="%d" rx="8" fill="#0d1117"/>' % (w, h),
        '<rect width="%d" height="%d" rx="8" fill="#161b22"/>' % (w, BAR),
        '<rect y="%d" width="%d" height="8" fill="#161b22"/>' % (BAR - 8, w),
        '<circle cx="18" cy="17" r="5.5" fill="#f85149"/><circle cx="36" cy="17" r="5.5" fill="#d29922"/>'
        '<circle cx="54" cy="17" r="5.5" fill="#3fb950"/>',
        '<text x="%d" y="22" fill="#8b949e" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
        'font-size="12.5" text-anchor="middle">%s</text>' % (w // 2, html.escape(title)),
        '<g fill="#e6edf3" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="14" '
        'xml:space="preserve">',
    ]
    y = BAR + PAD + 13
    for line in shown:
        colour = ""
        if line.startswith("$ "):
            colour = ' fill="#79c0ff"'
        elif re.search(r"\b(REFUSED|FAIL|failed|rejected|refused)\b", line):
            colour = ' fill="#ffa198"'
        elif re.search(r"\b(PASS|OK|passed|accepted|approved)\b", line):
            colour = ' fill="#7ee787"'
        out.append('<text x="%d" y="%d"%s>%s</text>' % (PAD, y, colour, html.escape(line)))
        y += LINE_H
    out.append("</g></svg>")
    return "\n".join(out) + "\n", cut


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--in", dest="source", type=pathlib.Path, help="a text file a run printed")
    src.add_argument("--run", help="a command to run here; its output (and its exit code) is what is drawn")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--width", type=int, default=110, help="characters per line (default 110)")
    ap.add_argument("--max-lines", type=int, default=60)
    args = ap.parse_args(argv)
    if args.run:
        done = subprocess.run(args.run, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        text = "$ " + args.run + "\n" + done.stdout + done.stderr
        if done.returncode:
            text += "(exit code %d)\n" % done.returncode
        title = args.title or args.run
    else:
        text = args.source.read_text(encoding="utf-8")
        title = args.title or args.source.name
    lines = text.splitlines()
    if len(lines) > args.max_lines:
        print("REFUSED: %d lines, above --max-lines %d; draw a shorter output" % (len(lines), args.max_lines),
              file=sys.stderr)
        return 2
    svg, cut = draw(lines, title, args.width)
    args.out.write_text(svg, encoding="utf-8", newline="\n")
    print("wrote %s | lines %d | lines cut at %d characters: %d" % (args.out.name, len(lines), args.width, cut))
    return 0


if __name__ == "__main__":
    sys.exit(main())
