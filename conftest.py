# Makes pytest add the repository root to sys.path so tests/ can import the pgtuner
# package — and pins PGTUNER_PROJECT_ROOT to the repo so the suite is hermetic:
# launching pytest from any other cwd no longer plants real .pgtuner state there
# (which would then hijack anchor-walk resolution for later repo-root runs; the
# R1-B adversarial-review finding).
import os
from pathlib import Path

os.environ.setdefault("PGTUNER_PROJECT_ROOT", str(Path(__file__).resolve().parent))
