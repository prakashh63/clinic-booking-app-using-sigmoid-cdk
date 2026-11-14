"""CDK entry point for the consumer application."""

import os
import sys
from pathlib import Path



from app_config import build_config  
from sigmoid_cdk import SigmoidApp  

app = SigmoidApp(build_config(os.environ.get("STAGE", "dev")))
app.synth()
