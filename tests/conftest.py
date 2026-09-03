"""Make the integration's Home-Assistant-free modules importable.

The package's ``__init__`` imports Home Assistant, which these tests
deliberately do not need. The authentication, portal, telemetry and redaction
code is plain ``aiohttp`` and is where every bug this integration has shipped
actually lived, so it is worth testing without a Home Assistant install in the
way. Binding the directory to a bare module loads those submodules without
running the package ``__init__``.

Coordinator, config-flow and entity behaviour do need Home Assistant. The CI
test lane installs one, so those tests run there; where none is installed they
skip themselves rather than failing the run.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_INTEGRATION = (
    Path(__file__).resolve().parents[1] / "custom_components" / "jlr_incontrol"
)

if "jlr" not in sys.modules:
    package = types.ModuleType("jlr")
    package.__path__ = [str(_INTEGRATION)]
    sys.modules["jlr"] = package
