"""CDK entry point for the consumer application."""

import os
import sys
from pathlib import Path

# `cdk.json` runs `python3 bin/app.py`; expose the consumer root for src/ imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_config import build_config  # noqa: E402
from sigmoid_cdk import SigmoidApp  # noqa: E402

app = SigmoidApp(build_config(os.environ.get("STAGE", "dev")))
app.synth()
