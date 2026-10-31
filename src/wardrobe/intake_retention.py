"""Protect private content and evidence retained by intake records."""

from sqlalchemy import select

from .models import (
    GarmentIntake,
    GarmentProductEvidence,
    IntakeProductEnrichment,
    IntakeSourceObject,
    ProductSnapshot,
)


def protected_hashes(session, now):
    hashes = set()
    for source in session.scalars(
        select(IntakeSourceObject).where(IntakeSourceObject.state != "collected")
    ):
        task = session.get(GarmentIntake, source.task_id)
        if source.state == "submitted" or task.state not in {"closed", "expired"}:
            hashes.update(
                item["sha256"] for item in source.report.get("outputs", {}).values()
            )
    return hashes


def finish_collection(session, files, now):
    entries = list(session.scalars(select(IntakeProductEnrichment)))
    persistent_refs = {
        identifier
        for evidence in session.scalars(select(GarmentProductEvidence))
        for identifier in evidence.snapshot_ids
    }
    for entry in entries:
        task = session.get(GarmentIntake, entry.task_id)
        if task.state not in {"closed", "expired"} or entry.updated_at + 86400 > now:
            continue
        # Remove only snapshots first created by this candidate. Reused rows
        # and snapshots referenced by another draft or garment survive.
        other_refs = {
            ref["snapshot_id"]
            for other in entries
            if other.task_id != entry.task_id
            for ref in other.payload.get("evidence", {}).get("snapshot_refs", [])
        }
        other_refs.update(
            request["value"]["snapshot_id"]
            for other in entries
            if other.task_id != entry.task_id
            for round in other.payload.get("rounds", [])
            for request in round["requests"]
            if isinstance(request.get("value"), dict)
            and "snapshot_id" in request["value"]
        )
        for identifier in entry.payload.get("created_snapshot_ids", []):
            if identifier not in persistent_refs | other_refs:
                snapshot = session.get(ProductSnapshot, identifier)
                if snapshot is not None:
                    session.delete(snapshot)
        rounds = []
        for value in entry.payload.get("rounds", []):
            rounds.append(
                {
                    **value,
                    "requests": [
                        {**request, "value": None} for request in value["requests"]
                    ],
                }
            )
        entry.payload = {"rounds": rounds, "collected": True}
        entry.reserved_bytes = 0
        entry.evidence_digest = None
        entry.state = "collected"
