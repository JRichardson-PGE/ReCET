"""recet -- ReCET, the rebuilt CEDARS Cost Effectiveness Tool: prices CET-format inputs hour by hour on the ACC.

    python -m recet                # opens the graphical interface
    recet run --measures Measure.csv --acc 2026 ...
    recet --help

See README.md at the repository root and `recet/docs/README.md`.
"""
import os as _os

# numpy's OpenBLAS reserves a work buffer for every CPU thread when numpy is first imported (0.5 GB of committed memory on a 16-thread machine), and ReCET's
# arithmetic is element by element, so the threads buy nothing: a 1,000,000-record run took the same time with one (README Performance). This only takes effect
# when recet is imported before numpy (every recet command, the window and the web app do); set OPENBLAS_NUM_THREADS yourself to choose otherwise.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "0.1.54"
