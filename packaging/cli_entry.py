import sys

from multilayer.cli import main

raise SystemExit(main(sys.argv[1:] or ["--help"]))
