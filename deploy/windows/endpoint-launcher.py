"""Run the installed endpoint in the interactive execution account."""
import os
import subprocess
import sys
from pathlib import Path
root=Path(os.environ.get("DESCRIPTION_ENDPOINT_ROOT", r"C:\description-v1-review"))
os.environ["GH_CONFIG_DIR"]=str(root/"github")
os.environ["PATH"]=str(root/"tools/bin")+os.pathsep+os.environ["PATH"]
os.environ["GIT_TERMINAL_PROMPT"]="0"
with (root/"reports/endpoint.log").open("a",encoding="utf-8") as log:
    result=subprocess.run([sys.executable,"-I","-u","-m","description_pipeline","serve","--config",str(root/"endpoint.json")],cwd=root,stdout=log,stderr=subprocess.STDOUT)
raise SystemExit(result.returncode)
