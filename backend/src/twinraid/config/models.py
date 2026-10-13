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
Minutes = Annotated[StrictInt, Field(ge=1, le=60)]


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Weekdays = Annotated[list[Weekday], Field(min_length=1), AfterValidator(reject_duplicate_days)]
TimeOfDay = Annotated[
    StrictStr, BeforeValidator(reject_unquoted_time), AfterValidator(validate_time_of_day)
]
TimeZoneName = Annotated[StrictStr, AfterValidator(validate_time_zone)]


class TlsConfig(StrictModel):
    """Adressen für das Zertifikat und die Host-Prüfung (Konzept 5.1 und 5.2)."""

    ip_addresses: Annotated[
        list[TlsAddress], Field(min_length=1), AfterValidator(reject_duplicates)
    ]
    hostnames: Annotated[
        list[Hostname], AfterValidator(reject_duplicates), Field(default_factory=list)
    ]

    def allowed_hosts(self) -> frozenset[str]:
        """Werte, die im Host-Header stehen dürfen (ohne Port)."""
        return frozenset([*(str(ip) for ip in self.ip_addresses), *self.hostnames])


class WebConfig(StrictModel):
    port: Annotated[StrictInt, Field(ge=1, le=65535)]
    allowed_networks: Annotated[list[IPv4Network | IPv6Network], Field(min_length=1)]
    session_idle_minutes: Annotated[StrictInt, Field(ge=1, le=720)]
    default_language: Literal["de", "en"]
    tls: TlsConfig


class SourceServerConfig(StrictModel):
    name: DisplayText
    host: HostAddress


class TargetServerConfig(StrictModel):
    name: DisplayText
    host: HostAddress
    mac: MacAddress
    wol_broadcast: IPv4Address
    boot_timeout_minutes: Minutes


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


class TwinRaidConfig(StrictModel):
    web: WebConfig
    servers: ServersConfig
    gaming_vm: GamingVmConfig
    sync: SyncConfig
