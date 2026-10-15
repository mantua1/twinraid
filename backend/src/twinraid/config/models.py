"""Pydantic-Modelle der Konfigurationsdatei twinraid.yaml (Konzept 6.2 und 6.3)."""

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from twinraid.config.validators import (
    normalize_mac,
    parse_allowed_network,
    reject_duplicate_days,
    reject_duplicates,
    reject_unquoted_time,
    validate_container_name,
    validate_display_text,
    validate_host_address,
    validate_hostname,
    validate_relative_path,
    validate_share_name,
    validate_time_of_day,
    validate_time_zone,
    validate_tls_address,
    validate_vm_name,
)
from twinraid.shared.base import StrictModel

ShareName = Annotated[StrictStr, AfterValidator(validate_share_name)]
RelativePath = Annotated[StrictStr, AfterValidator(validate_relative_path)]
ContainerName = Annotated[StrictStr, AfterValidator(validate_container_name)]
DisplayText = Annotated[StrictStr, AfterValidator(validate_display_text)]
VmName = Annotated[StrictStr, AfterValidator(validate_vm_name)]
MacAddress = Annotated[StrictStr, AfterValidator(normalize_mac)]
HostAddress = Annotated[IPv4Address | IPv6Address, AfterValidator(validate_host_address)]
TlsAddress = Annotated[IPv4Address | IPv6Address, AfterValidator(validate_tls_address)]
Hostname = Annotated[StrictStr, AfterValidator(validate_hostname)]
# Ganzes Netz oder einzelnes Gerät (/32, /128 oder ohne Maske), siehe Konzept 5.1.
AllowedNetwork = Annotated[IPv4Network | IPv6Network, BeforeValidator(parse_allowed_network)]
Minutes = Annotated[StrictInt, Field(ge=1, le=60)]
Port = Annotated[StrictInt, Field(ge=1, le=65535)]

# Standardwert für den Inaktivitäts-Timeout der Sessions (Konzept 5.3).
DEFAULT_SESSION_IDLE_MINUTES = 10
# Port der Weboberfläche; passt zum Unraid-Template (Konzept 6.3).
DEFAULT_WEB_PORT = 8443
DEFAULT_SSH_PORT = 22


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Weekdays = Annotated[list[Weekday], Field(min_length=1), AfterValidator(reject_duplicate_days)]
TimeOfDay = Annotated[
    StrictStr, BeforeValidator(reject_unquoted_time), AfterValidator(validate_time_of_day)
]
TimeZoneName = Annotated[StrictStr, AfterValidator(validate_time_zone)]


class TlsConfig(StrictModel):
    """Adressen für das Zertifikat und die Host-Prüfung (Konzept 5.1 und 5.2).

    ip_addresses ist optional: Ohne Angabe erkennt twinraid-init die IP des
    Containers (shared/addresses.py). Die verwendeten Adressen liefert
    resolve_addresses(), nicht dieses Modell.
    """

    ip_addresses: (
        Annotated[list[TlsAddress], Field(min_length=1), AfterValidator(reject_duplicates)] | None
    ) = None
    hostnames: Annotated[
        list[Hostname], AfterValidator(reject_duplicates), Field(default_factory=list)
    ]


class WebConfig(StrictModel):
    port: Port = DEFAULT_WEB_PORT
    allowed_networks: Annotated[list[AllowedNetwork], Field(min_length=1)]
    session_idle_minutes: Annotated[StrictInt, Field(ge=1, le=720)] = DEFAULT_SESSION_IDLE_MINUTES
    default_language: Literal["de", "en"]
    tls: TlsConfig


class WebGuiConfig(StrictModel):
    """WebGUI eines Unraid-Servers: Ziel des TCP-Checks und Adresse der API (Konzept 3.5).

    scheme entspricht der Unraid-Einstellung „SSL/TLS verwenden“: Nein → http, Ja → https.
    """

    scheme: Literal["http", "https"]
    port: Port | None = None

    @property
    def effective_port(self) -> int:
        if self.port is not None:
            return self.port
        return 443 if self.scheme == "https" else 80

    @property
    def encrypted(self) -> bool:
        return self.scheme == "https"


class SourceServerConfig(StrictModel):
    name: DisplayText
    host: HostAddress
    webgui: WebGuiConfig


class TargetServerConfig(StrictModel):
    name: DisplayText
    host: HostAddress
    webgui: WebGuiConfig
    mac: MacAddress
    # Optional: ohne Angabe leitet der Worker die Broadcast-Adresse aus dem eigenen
    # Netz ab, sofern Groß darin liegt (Konzept 4.2).
    wol_broadcast: IPv4Address | None = None
    boot_timeout_minutes: Minutes
    # SSH für Herunterfahren und VM-Shutdown mit dem Key „power“ (Konzept 5.6).
    ssh_port: Port = DEFAULT_SSH_PORT


class ServersConfig(StrictModel):
    source: SourceServerConfig
    target: TargetServerConfig

    @model_validator(mode="after")
    def _distinct_hosts(self) -> Self:
        if self.source.host == self.target.host:
            raise ValueError("source and target must have different host addresses")
        return self


class GamingVmConfig(StrictModel):
    display_name: DisplayText
    unraid_vm_name: VmName
    shutdown_timeout_minutes: Minutes


class ScheduleConfig(StrictModel):
    """Zeitplan für den Sync (Konzept 4.8). Die Uhrzeit gilt in der angegebenen Zeitzone."""

    enabled: StrictBool
    timezone: TimeZoneName
    days: Weekdays
    time: TimeOfDay
    wake_target: StrictBool
    shutdown_after: StrictBool


class SyncConfig(StrictModel):
    shares: Annotated[list[ShareName], Field(min_length=1)]
    appdata_share: ShareName
    exclude: list[RelativePath]
    protect_on_target: list[RelativePath]
    max_deletions: Annotated[StrictInt, Field(ge=0)]
    stop_containers: StrictBool
    never_stop: list[ContainerName]
    copy_templates: StrictBool
    schedule: ScheduleConfig

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        folded = [share.casefold() for share in self.shares]
        duplicates = sorted({s for s in self.shares if folded.count(s.casefold()) > 1})
        if duplicates:
            raise ValueError(f"shares contains duplicates: {', '.join(duplicates)}")
        if self.appdata_share not in self.shares:
            raise ValueError(f"appdata_share {self.appdata_share!r} must be listed in shares")

        shares = set(self.shares)
        for field_name, paths in (
            ("exclude", self.exclude),
            ("protect_on_target", self.protect_on_target),
        ):
            for path in paths:
                first = path.split("/", 1)[0]
                if first not in shares:
                    raise ValueError(
                        f"{field_name} entry {path!r} must start with one of the configured "
                        f"shares ({', '.join(self.shares)})"
                    )
        for path in self.exclude:
            if "/" not in path:
                raise ValueError(
                    f"exclude entry {path!r} would exclude a whole share; "
                    "remove it from shares instead"
                )
        return self


class ActionsConfig(StrictModel):
    """Aktionen im Modus real einzeln freischalten (Konzept 4.2, Entscheidung E9).

    Standard ist überall aus. Im Simulationsmodus sind alle Aktionen verfügbar.
    """

    wake: StrictBool = False
    vm: StrictBool = False
    shutdown: StrictBool = False


class TwinRaidConfig(StrictModel):
    web: WebConfig
    servers: ServersConfig
    gaming_vm: GamingVmConfig
    sync: SyncConfig
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
