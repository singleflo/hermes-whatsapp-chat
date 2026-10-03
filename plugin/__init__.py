"""hermes-whatsapp-chat agent half.

Present so Hermes recognises this package as a unified Agent + Desktop plugin
(Install from Git needs plugin.yaml and __init__.py side by side). It registers
nothing: the plugin's surfaces are the dashboard router (dashboard/plugin_api.py),
the desktop page (desktop/plugin.js) and the WhatsApp channel service (sidecar/).
"""


def register(ctx) -> None:  # noqa: ARG001 - Hermes plugin entry point
    return None
