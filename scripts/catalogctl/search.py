"""Elasticsearch alias generations and the search rebuild."""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from .backup import BackupOperations
from .common import CatalogError, log, utc_now
from .constants import DEPENDENCY_SERVICES, MANAGED_ALIASES


GENERATION_STAMP_RE = re.compile(r"-(\d{8})t(\d{6})(\d{6})z$")


def generation_built_at(index_name: str) -> str | None:
    """The UTC instant a generation index was built, from its name, as an ISO timestamp."""
    match = GENERATION_STAMP_RE.search(index_name)
    if not match:
        return None
    day, clock, micro = match.groups()
    return f"{day[:4]}-{day[4:6]}-{day[6:]}T{clock[:2]}:{clock[2:4]}:{clock[4:]}.{micro}Z"


class SearchOperations(BackupOperations):
    """Search generations are rebuilt beside the live ones and switched atomically by alias."""

    def retained_generations(self) -> list[str]:
        """Generation indices the active and rollback releases still point at; a rebuild must not prune them."""
        names: set[str] = set()
        for state in (self.active_state(), self.read_rollback()):
            names.update((state or {}).get("search_aliases", {}).values())
        return sorted(names)

    def resync_search_since(self, compose_file: Path, aliases: dict[str, str]) -> None:
        """Re-indexes what changed since a release's generations were built, so a rollback does not serve stale search."""
        stamps = [stamp for stamp in map(generation_built_at, aliases.values()) if stamp]
        if stamps:
            self.manage(compose_file, "resync_search_documents", "--since", min(stamps))

    def alias_targets(self, compose_file: Path) -> dict[str, str]:
        result = self.compose(
            compose_file,
            "exec",
            "-T",
            "elasticsearch",
            "curl",
            "-fsS",
            "http://localhost:9200/_alias",
        )
        payload = json.loads(result.stdout or "{}")
        targets: dict[str, str] = {}
        for index_name, index_data in payload.items():
            for alias in index_data.get("aliases", {}):
                if alias in MANAGED_ALIASES or alias.startswith("autocomplete_"):
                    targets[alias] = index_name
        return targets

    def set_alias_targets(self, compose_file: Path, targets: dict[str, str]) -> None:
        current = self.alias_targets(compose_file)
        actions: list[dict[str, dict[str, str]]] = []
        for alias, index_name in current.items():
            if alias in targets or alias in MANAGED_ALIASES or alias.startswith("autocomplete_"):
                actions.append({"remove": {"index": index_name, "alias": alias}})
        for alias, index_name in targets.items():
            actions.append({"add": {"index": index_name, "alias": alias}})
        if not actions:
            return
        body = json.dumps({"actions": actions})
        self.compose(
            compose_file,
            "exec",
            "-T",
            "elasticsearch",
            "curl",
            "-fsS",
            "-XPOST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            body,
            "http://localhost:9200/_aliases",
        )

    def rebuild(self) -> dict[str, Any]:
        with self.locked():
            candidate = self.candidate_state()
            if candidate["migration_status"] != "succeeded":
                raise CatalogError("migrate must succeed before rebuild")
            if candidate["data_rebuild_status"] == "succeeded":
                return {
                    "phase": "rebuild",
                    "ok": True,
                    "already": True,
                    "aliases": candidate.get("search_aliases_after", {}),
                }
            if candidate["data_rebuild_status"] != "pending":
                raise CatalogError(f"candidate rebuild status is {candidate['data_rebuild_status']}")
            self.require_writers_drained(candidate, self.active_state())
            compose_file = Path(candidate["compose_file"])
            self.compose(compose_file, "up", "-d", "--wait", *DEPENDENCY_SERVICES, capture=False)
            before = self.alias_targets(compose_file)
            candidate = self.update_candidate(
                candidate,
                data_rebuild_status="in_progress",
                search_aliases_before=before,
                data_rebuild_started_at=utc_now(),
            )
            try:
                retained = self.retained_generations()
                self.manage(compose_file, "rebuild_es_index", *(["--retain", ",".join(retained)] if retained else []))
                self.manage(compose_file, "validate_search_indexes")
                self.manage(compose_file, "populate_visualization_cache", "--clear")
                after = self.alias_targets(compose_file)
                if not after:
                    raise CatalogError("search rebuild produced no managed aliases")
            except Exception as error:
                with contextlib.suppress(Exception):
                    self.set_alias_targets(compose_file, before)
                self.update_candidate(
                    candidate,
                    data_rebuild_status="failed",
                    data_rebuild_failed_at=utc_now(),
                    data_rebuild_error=str(error),
                )
                raise
            self.update_candidate(
                candidate,
                data_rebuild_status="succeeded",
                data_rebuild_completed_at=utc_now(),
                search_aliases_after=after,
            )
            log("Search indexes and visualization cache rebuilt and validated")
            return {"phase": "rebuild", "ok": True, "aliases": after}
