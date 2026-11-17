"""English help table for PGTUNER_LANG=en (applied by pgtuner/i18n.py).

Keys are command-path tuples: () = root group, ("instance",) = group,
("instance", "register") = command, ("instance", "register", "--port") =
option, and a trailing "__epilog__" element marks an epilog. The Chinese
docstrings in pgtuner/cli.py stay the single source of truth; these modules
are pure translations, merged from every module found in this package.
"""
import importlib
import pkgutil


def _merge() -> dict:
    table: dict = {}
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")
        table.update(getattr(module, "HELP"))
    return table


HELP = _merge()
