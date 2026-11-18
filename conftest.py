# Makes pytest add the repository root to sys.path so tests/ can import the pgtuner
# package — and pins PGTUNER_PROJECT_ROOT to the repo so the suite is hermetic:
# launching pytest from any other cwd no longer plants real .pgtuner state there
# (which would then hijack anchor-walk resolution for later repo-root runs; the
# R1-B adversarial-review finding).
import os
from pathlib import Path

os.environ.setdefault("PGTUNER_PROJECT_ROOT", str(Path(__file__).resolve().parent))


def cli_runner():
    """CliRunner with SEPARATE stdout/stderr on every click we support.
    py3.9 resolves click 8.1 (8.2 dropped py3.9): streams merge by default
    there, so .stderr raises and stdout is not machine-readable — those
    calls need mix_stderr=False. click >= 8.2 always separates and rejects
    the kwarg (TypeError). Tests asserting pure-stdout JSON or reading
    .stderr must construct their runner through this helper (first seen on
    the CI 3.9 leg, run 44206869866 — 3.9 had never executed the suite
    before, its setup-python step used to fail earlier).
    click < 8.2 also loses raw stderr writes: invoke()'s cleanup flushes
    sys.stdout ONLY, then reads both buffers — small `print(...,
    file=sys.stderr)` sits in the stderr TextIOWrapper's text buffer and
    never reaches the BytesIO the Result reads (click >= 8.2 rewrote the
    stream plumbing; there raw writes survive). Reproduced with click
    8.1.8 on py3.12: a command whose only stderr write is a raw print
    loses it; a later click.echo(err=True) masks the bug by flushing the
    shared wrapper. Hence the subclass below: reconfigure the isolated
    stderr to write_through so every write lands in the buffer directly."""
    import sys
    from contextlib import contextmanager
    from click.testing import CliRunner

    class _WriteThroughCliRunner(CliRunner):
        def isolation(self, *args, **kwargs):
            @contextmanager
            def _wrapper(*a, **k):
                with super(_WriteThroughCliRunner, self).isolation(*a, **k) as streams:
                    if streams[1] is not None:
                        sys.stderr.reconfigure(write_through=True)  # click < 8.2 flushes stdout only
                    yield streams
            return _wrapper(*args, **kwargs)

    try:
        return _WriteThroughCliRunner(mix_stderr=False)   # click < 8.2
    except TypeError:
        return CliRunner()                                # click >= 8.2: always separate
