"""Basisklasse für alle Pydantic-Modelle von TwinRaid."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Unveränderliches Modell, das unbekannte Felder ablehnt.

    Unbekannte Felder sind immer ein Fehler: In der Konfiguration fallen so
    Tippfehler auf, im Befehlsprotokoll werden keine zusätzlichen Parameter
    eingeschleust.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
