"""tapo_monitor — comprehensive, config-driven monitoring for TP-Link Tapo PTZ cameras.

The package consolidates what previously lived in separate per-deployment scripts into
one library plus a single config-driven daemon. Capabilities (detection sources,
auto-tracking, scheduling, weather gating, AI enrichment, notifications, multi-camera
coordination) are opt-in per camera via ``cameras.yaml``.

No personal data lives in this package: coordinates, hosts and secrets all come from
configuration/environment.
"""

__version__ = "0.5.0"
