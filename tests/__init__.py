"""AutoShorts test package.

Sets an isolated AUTOSHORTS_DATA *before* any autoshorts import so tests never
touch a developer's real state file.
"""
import os
import tempfile

if "AUTOSHORTS_DATA" not in os.environ or "as-test" not in os.environ["AUTOSHORTS_DATA"]:
    os.environ["AUTOSHORTS_DATA"] = tempfile.mkdtemp(prefix="as-test-")
