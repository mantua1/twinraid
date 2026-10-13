"""Konfigurationswerte, die nie in öffentlichen Antworten stehen dürfen (Konzept 5.3).

Öffentlich sind nur /health und die Dateien der Oberfläche. Server-, Netz- und
Share-Daten gibt es ausschließlich über geschützte Routen unter /api. Tests
durchsuchen alle öffentlichen Antworten nach diesen Werten.
"""

from twinraid.config.models import TwinRaidConfig


def sensitive_values(config: TwinRaidConfig) -> frozenset[str]:
    """Alle schützenswerten Werte als Text, inklusive üblicher Schreibweisen der MAC."""
    values: set[str] = set()
    for network in config.web.allowed_networks:
        values.add(str(network))
        values.add(str(network.network_address))

    source = config.servers.source
    target = config.servers.target
    values.update({str(source.host), str(target.host), str(target.wol_broadcast)})
    values.update({target.mac, target.mac.replace(":", "-"), target.mac.replace(":", "")})
    values.update({source.name, target.name})
    values.update({config.gaming_vm.display_name, config.gaming_vm.unraid_vm_name})

    sync = config.sync
    values.update(sync.shares)
    values.add(sync.appdata_share)
    values.update(sync.exclude)
    values.update(sync.protect_on_target)
    values.update(sync.never_stop)
    return frozenset(values)
