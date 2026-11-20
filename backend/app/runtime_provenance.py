"""Non-secret implementation and installed distribution identifiers for run audits."""

import platform
from functools import lru_cache
from importlib.metadata import distributions

from app.agent_tools import SOURCE_BUNDLE_ID
from app.tool_schemas import TOOLS
from app.values import digest


@lru_cache(maxsize=1)
def runtime_provenance():
    packages = sorted(
        {(d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"]}
    )
    contracts = {
        name: digest([name, schema.model_json_schema(), description])
        for name, (schema, description) in sorted(TOOLS.items())
    }
    return {
        "source_bundle_id": SOURCE_BUNDLE_ID,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.system(),
        "packages": [{"name": name, "version": version} for name, version in packages],
        "tool_contracts": contracts,
    }
