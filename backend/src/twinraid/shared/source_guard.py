"""Status des Wächters auf Klein, wie ihn get_status liefert (Konzept 3.6, ab v0.5.0).

Die Codes sind die festen Listen aus host-scripts/source-guard.sh. Ein Test gleicht sie
mit dem Skript ab. Container- und Dataset-Namen sind Text von Klein; sie folgen den
Zeichenregeln von Docker bzw. von Share-Namen, kommen nur über geschützte Routen und
stehen nie im Log.
"""

from datetime import datetime
from enum import StrEnum

from twinraid.shared.base import StrictModel


class GuardState(StrEnum):
    """Zustand des Wächters aus Sicht von TwinRaid (Konzept 3.6, „Lesen in TwinRaid“)."""

    ACTIVE = "active"
    # Keiner der beiden Pfade eingebunden: kein Fehler, der Wächter ist nicht eingerichtet.
    NOT_MOUNTED = "not_mounted"
    # status.json fehlt oder der Herzschlag ist älter als 30 Sekunden.
    INACTIVE = "inactive"
    # Der Wächter meldet selbst einen Fehler (GuardError).
    ERROR = "error"
    # TwinRaid findet die Einbindungen falsch vor (GuardSetupProblem).
    MISCONFIGURED = "misconfigured"
    # status.json ist nicht lesbar oder verletzt das Schema (GuardStatusProblem).
    INVALID_STATUS = "invalid_status"


class GuardSetupProblem(StrEnum):
    """Falsche Einbindung, festgestellt von TwinRaid selbst."""

    STATE_NOT_MOUNTED = "state_not_mounted"
    REQUEST_NOT_MOUNTED = "request_not_mounted"
    STATE_WRITABLE = "state_writable"
    REQUEST_READ_ONLY = "request_read_only"
    # Docker hat ein Verzeichnis angelegt, weil TwinRaid vor dem Wächter startete.
    REQUEST_IS_DIRECTORY = "request_is_directory"
    REQUEST_NOT_REGULAR = "request_not_regular"


class GuardStatusProblem(StrEnum):
    """Was an status.json nicht stimmt."""

    NOT_REGULAR = "not_regular"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    BAD_JSON = "bad_json"
    SCHEMA = "schema"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    HEARTBEAT_FUTURE = "heartbeat_future"


class GuardError(StrEnum):
    """Feld error in status.json."""

    CONFIG_INVALID = "config_invalid"
    MISCONFIGURED = "misconfigured"
    REQUEST_NOT_A_FILE = "request_not_a_file"
    STATE_LOST = "state_lost"
    DOCKER_UNAVAILABLE = "docker_unavailable"


class GuardErrorDetail(StrEnum):
    """Feld error_detail in status.json."""

    # config_invalid: Fehler in source-guard.conf
    MISSING_FILE = "missing_file"
    BAD_OWNER = "bad_owner"
    TOO_LONG = "too_long"
    SYNTAX = "syntax"
    DUPLICATE = "duplicate"
    BAD_VALUE = "bad_value"
    FORBIDDEN_DATASET = "forbidden_dataset"
    TOO_MANY = "too_many"
    UNKNOWN_KEY = "unknown_key"
    ALLOW_CONTAINS_TWINRAID = "allow_contains_twinraid"
    # misconfigured: Ordner und tmpfs unter /run/twinraid-source-guard
    BASE_UNUSABLE = "base_unusable"
    PRIVATE_UNUSABLE = "private_unusable"
    STATE_UNUSABLE = "state_unusable"
    IN_UNUSABLE = "in_unusable"
    IN_UNEXPECTED_CONTENT = "in_unexpected_content"
    MOUNT_FAILED = "mount_failed"
    IN_WRONG_MOUNT = "in_wrong_mount"
    REQUEST_CREATE_FAILED = "request_create_failed"
    # request_not_a_file: die Auftragsdatei
    REQUEST_IS_DIRECTORY = "request_is_directory"
    REQUEST_IS_SYMLINK = "request_is_symlink"
    REQUEST_NOT_REGULAR = "request_not_regular"
    REQUEST_BAD_OWNER = "request_bad_owner"
    REQUEST_HARDLINK = "request_hardlink"


class GuardContainerState(StrEnum):
    """Zustand eines freigegebenen Containers (allowed[].state)."""

    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    # TwinRaid selbst steht in allow; der Wächter meldet dann allow_contains_twinraid.
    SELF = "self"
    UNKNOWN = "unknown"


class GuardRunEnd(StrEnum):
    """Grund für das Ende eines Laufs (last_run.reason)."""

    FINISH = "finish"
    DEADMAN_HEARTBEAT = "deadman_heartbeat"
    DEADMAN_MAX = "deadman_max"
    STOP_COMMAND = "stop_command"


class GuardRestart(StrEnum):
    """Ergebnis je Container beim Ende eines Laufs (last_run.containers[].result)."""

    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    CHANGED = "changed"
    GONE = "gone"
    START_FAILED = "start_failed"


class GuardOrder(StrEnum):
    BEGIN = "begin"
    STOP = "stop"
    PING = "ping"
    SNAPSHOT = "snapshot"
    FINISH = "finish"
    PREPARE = "prepare"
    CONFIRM = "confirm"
    SEND = "send"
    CLOSE = "close"


class GuardOrderResult(StrEnum):
    """Feld last.result (Konzept 3.6, „Ergebnisse“)."""

    NOT_SUPPORTED = "not_supported"
    CONFIG_INVALID = "config_invalid"
    MISCONFIGURED = "misconfigured"
    REQUEST_NOT_A_FILE = "request_not_a_file"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    OPENED = "opened"
    RUN_OPEN = "run_open"
    TOO_SOON = "too_soon"
    EXTENDED = "extended"
    NO_RUN = "no_run"
    FINISHED = "finished"
    STOPPED = "stopped"
    NOT_RUNNING = "not_running"
    NOT_ALLOWED = "not_allowed"
    DENIED_SELF = "denied_self"
    NOT_FOUND = "not_found"
    STOP_FAILED = "stop_failed"
    # Ab Version 2 (Konzept 3.6, „Job, Prüfen und Senden“).
    JOB_OPEN = "job_open"
    CREATED = "created"
    ZFS_UNAVAILABLE = "zfs_unavailable"
    NO_DATASETS = "no_datasets"
    DATASET_MISSING = "dataset_missing"
    SNAPSHOT_LOW_SPACE = "snapshot_low_space"
    SNAPSHOT_LIMIT = "snapshot_limit"
    SNAPSHOT_FAILED = "snapshot_failed"
    PREPARING = "preparing"
    NO_JOB = "no_job"
    WRONG_STATE = "wrong_state"
    CONFIRMED = "confirmed"
    NOT_NEEDED = "not_needed"
    SENDING = "sending"
    NOT_READY = "not_ready"
    CLOSED = "closed"


class GuardJobState(StrEnum):
    """Zustand des Jobs (Konzept 3.6); offen sind die ersten fünf."""

    SNAPSHOT = "snapshot"
    PREPARING = "preparing"
    PREPARED = "prepared"
    AWAITING_CONFIRM = "awaiting_confirm"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"
    CLOSED = "closed"


class GuardJobResult(StrEnum):
    OK = "ok"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"
    JOB_TIMEOUT = "job_timeout"
    PREPARE_FAILED = "prepare_failed"
    SEND_FAILED = "send_failed"
    TARGET_REFUSED = "target_refused"
    TARGET_MODIFIED = "target_modified"


class GuardCheckResult(StrEnum):
    """Ergebnis von guard.sh sync check über SSH; Meldungen von ssh gehen nie weiter."""

    OK = "ok"
    TARGET_NOT_CONFIGURED = "target_not_configured"
    KEY_MISSING = "key_missing"
    HOST_UNTRUSTED = "host_untrusted"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    DENIED = "denied"
    SSH_FAILED = "ssh_failed"
    BAD_ANSWER = "bad_answer"


class GuardJobReason(StrEnum):
    """job.reason: Grund von guard.sh (3.7, mit "_"), ein Fehler von ssh oder auf Klein."""

    NAME_DENIED = "name_denied"
    RECEIVE_BUSY = "receive_busy"
    MIRROR_ROOT_MISSING = "mirror_root_missing"
    RECEIVER_ID_IN_USE = "receiver_id_in_use"
    TARGET_EXISTS = "target_exists"
    DELEGATION_MISMATCH = "delegation_mismatch"
    PROPS_MISMATCH = "props_mismatch"
    PROPS_NOT_APPLIED = "props_not_applied"
    CLONE_ACTIVE = "clone_active"
    SPACE_LOW = "space_low"
    TOO_SOON = "too_soon"
    RECEIVED_PROPERTIES = "received_properties"
    MOUNT_MISMATCH = "mount_mismatch"
    REFUSED_OTHER = "refused_other"
    KEY_MISSING = "key_missing"
    HOST_UNTRUSTED = "host_untrusted"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    DENIED = "denied"
    SSH_FAILED = "ssh_failed"
    BASE_MISSING = "base_missing"
    NOT_READY = "not_ready"
    SELF_DATASET_MISSING = "self_dataset_missing"
    SEND_ERROR = "send_error"


class GuardBlocker(StrEnum):
    """Warum ein vorbereiteter Job nicht gesendet werden kann (send_ready false)."""

    TARGET_UNREACHABLE = "target_unreachable"
    MIRROR_ROOT_MISSING = "mirror_root_missing"
    MIRROR_CONF_INVALID = "mirror_conf_invalid"
    DELEGATION_MISMATCH = "delegation_mismatch"
    RECEIVE_BUSY = "receive_busy"
    CLONE_ACTIVE = "clone_active"
    SPACE_LOW = "space_low"
    BASE_MISSING = "base_missing"
    TARGET_EXISTS = "target_exists"
    NOT_IN_MIRROR_CONF = "not_in_mirror_conf"
    SELF_DATASET_MISSING = "self_dataset_missing"
    PROPS_MISMATCH = "props_mismatch"


class GuardConfirmReason(StrEnum):
    DELETIONS = "deletions"
    SHRUNK = "shrunk"
    NO_DIFF = "no_diff"


class GuardDatasetProblem(StrEnum):
    BASE_MISSING = "base_missing"
    NOT_IN_MIRROR_CONF = "not_in_mirror_conf"
    TARGET_EXISTS = "target_exists"
    SELF_DATASET_MISSING = "self_dataset_missing"


class GuardDatasetResult(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    BASE_MISSING = "base_missing"
    NOT_READY = "not_ready"
    SEND_FAILED = "send_failed"
    TARGET_REFUSED = "target_refused"
    TARGET_MODIFIED = "target_modified"


class GuardZfsState(StrEnum):
    OK = "ok"
    # Kein mainpool oder zfs antwortet nicht; Anzeige als zfs_unavailable.
    UNAVAILABLE = "unavailable"


class GuardSelfDataset(StrEnum):
    """Eigenes Dataset von TwinRaid (E18)."""

    FOUND = "found"
    MISSING = "missing"
    # /config oder /data liegen nicht in mainpool/appdata/twinraid.
    OUTSIDE = "outside"
    # Der Wächter findet keinen TwinRaid-Container (nach Image oder Label).
    NO_CONTAINER = "no_container"
    UNKNOWN = "unknown"


class GuardContainer(StrictModel):
    name: str
    state: GuardContainerState


class GuardRun(StrictModel):
    """Offener Lauf: angehaltene Container und Fristen (als Uhrzeit)."""

    opened_at: datetime
    heartbeat_deadline: datetime
    max_deadline: datetime
    stopped: tuple[str, ...]


class GuardRestartResult(StrictModel):
    name: str
    result: GuardRestart


class GuardLastRun(StrictModel):
    opened_at: datetime
    closed_at: datetime
    reason: GuardRunEnd
    containers: tuple[GuardRestartResult, ...]


class GuardDataset(StrictModel):
    name: str
    # None: unbekannt, weil ZFS nicht verfügbar ist.
    present: bool | None
    referenced: int | None
    compression: str | None


class GuardZfs(StrictModel):
    state: GuardZfsState
    pool_used: int | None
    pool_available: int | None
    datasets: tuple[GuardDataset, ...]
    self_dataset: GuardSelfDataset


class GuardLimits(StrictModel):
    max_stop_minutes: int
    heartbeat_timeout_seconds: int
    stop_timeout_seconds: int
    min_run_interval_minutes: int
    max_deletions: int
    snapshot_keep_days: int
    snapshot_reserve_percent: int


class GuardDetails(StrictModel):
    """Inhalt eines frischen Status (Herzschlag höchstens 30 s alt, auch bei error)."""

    version: int
    started_at: datetime
    allowed: tuple[GuardContainer, ...]
    run: GuardRun | None
    last_run: GuardLastRun | None
    # Frühester nächster Lauf (Pause zwischen zwei Läufen); None, wenn keine Pause gilt.
    run_allowed_at: datetime | None
    zfs: GuardZfs
    limits: GuardLimits
    target_configured: bool
    target_port: int
    rejected_count: int


class SourceGuardStatus(StrictModel):
    state: GuardState
    # Nur bei misconfigured bzw. invalid_status.
    setup_problem: GuardSetupProblem | None = None
    status_problem: GuardStatusProblem | None = None
    # Fehler des Wächters (bei error, oder aus einem frischen Status neben misconfigured).
    error: GuardError | None = None
    error_detail: GuardErrorDetail | None = None
    # Zeile in source-guard.conf bei config_invalid; 0, wenn die Datei selbst fehlt.
    config_line: int | None = None
    # Letzter bekannter Herzschlag; bei inactive „nicht aktiv seit“.
    heartbeat_at: datetime | None = None
    # Alter des Herzschlags beim Aufruf von get_status, nur bei frischem Status.
    heartbeat_age_seconds: int | None = None
    details: GuardDetails | None = None


GUARD_NOT_READ = SourceGuardStatus(state=GuardState.INACTIVE)
