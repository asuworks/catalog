from __future__ import annotations

import getpass
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import BinaryIO
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("catalogctl", ROOT / "scripts" / "catalogctl.py")
assert SPEC and SPEC.loader
catalogctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalogctl
SPEC.loader.exec_module(catalogctl)


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.layout = catalogctl.Layout(root / "etc", root / "var", root / "backups")
        self.controller = catalogctl.Controller(ROOT, self.layout)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provision(self, host_id: str = "staging") -> None:
        self.controller.host_provision(host_id, getpass.getuser())

    def write_state(self, path: Path, **values: object) -> dict[str, object]:
        state = {"schema_version": catalogctl.SCHEMA_VERSION, **values}
        catalogctl.atomic_json(path, state)
        return state

    def write_candidate(self, **overrides: object) -> dict[str, object]:
        compose = self.layout.releases / "candidate" / "docker-compose.yml"
        compose.parent.mkdir(parents=True, exist_ok=True)
        compose.write_text("name: catalog\nservices: {}\n", encoding="utf-8")
        values: dict[str, object] = {
            "host_id": "staging",
            "operation_id": "operation-1",
            "release_id": "release-1",
            "image": "ghcr.io/comses/catalog@sha256:" + "a" * 64,
            "bundle_revision": "b" * 40,
            "citation_revision": "c" * 40,
            "release_dir": str(compose.parent),
            "compose_file": str(compose),
            "compose_sha256": catalogctl.sha256_file(compose),
            "bundle_sha256": catalogctl.sha256_tree(compose.parent, excluded={"docker-compose.yml"}),
            "support_images": ["postgres@example", "redis@example"],
            "created_at": catalogctl.utc_now(),
            "migration_status": "unmigrated",
            "data_rebuild_status": "pending",
            "restore_status": "succeeded",
            "search_aliases_after": {"publication": "publication-new"},
        }
        values.update(overrides)
        return self.write_state(self.layout.candidate, **values)

    def test_digest_validation_rejects_tags_and_malformed_digests(self) -> None:
        valid = "ghcr.io/comses/catalog@sha256:" + "a" * 64
        self.assertEqual(catalogctl.validate_image(valid), valid)
        for invalid in (
            "ghcr.io/comses/catalog:latest",
            "ghcr.io/comses/catalog:sha-deadbeef",
            "ghcr.io/comses/catalog@sha256:abc",
            "ghcr.io/comses/catalog@sha256:" + "A" * 64,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(catalogctl.CatalogError):
                catalogctl.validate_image(invalid)

    def test_provision_creates_fixed_identity_and_does_not_rotate_secrets(self) -> None:
        self.provision()
        password = (self.layout.secrets / "postgres_password").read_bytes()
        config = (self.layout.secrets / "config.ini").read_bytes()

        self.provision()

        self.assertEqual((self.layout.secrets / "postgres_password").read_bytes(), password)
        self.assertEqual((self.layout.secrets / "config.ini").read_bytes(), config)
        self.assertEqual(self.controller.host().host_id, "staging")
        with self.assertRaisesRegex(catalogctl.CatalogError, "identity is immutable"):
            self.controller.host_provision("prod", getpass.getuser())

    def test_max_map_count_configuration_sets_a_minimum_without_reducing_higher_values(self) -> None:
        current_path = Path(self.temporary.name) / "max_map_count"
        config_path = Path(self.temporary.name) / "99-comses-catalog.conf"
        for current, expected in ((65530, 262144), (1048576, 1048576)):
            with self.subTest(current=current):
                current_path.write_text(f"{current}\n", encoding="utf-8")
                with (
                    mock.patch.object(catalogctl, "MAX_MAP_COUNT_PATH", current_path),
                    mock.patch.object(catalogctl, "MAX_MAP_COUNT_CONFIG", config_path),
                    mock.patch.object(self.controller.runner, "run") as run,
                ):
                    self.controller.configure_max_map_count()

                self.assertEqual(config_path.read_text(encoding="utf-8"), f"vm.max_map_count={expected}\n")
                run.assert_called_once_with(
                    ["sysctl", "-w", f"vm.max_map_count={expected}"],
                    capture=False,
                )

    def test_host_rejects_writable_identity_file(self) -> None:
        self.provision()
        self.layout.host_env.chmod(0o666)
        with self.assertRaisesRegex(catalogctl.CatalogError, "unsafe permissions"):
            self.controller.host()

    def test_host_rejects_mismatched_database_password_files(self) -> None:
        self.provision()
        (self.layout.secrets / "postgres_password").write_text("different\n", encoding="utf-8")

        with self.assertRaisesRegex(catalogctl.CatalogError, "database passwords differ"):
            self.controller.host_check()

    def test_production_host_requires_complete_smtp_configuration(self) -> None:
        self.provision("prod")

        with self.assertRaisesRegex(
            catalogctl.CatalogError,
            "email.EMAIL_HOST_USER, email.EMAIL_HOST_PASSWORD",
        ):
            self.controller.host_check()

    def test_candidate_records_exact_image_bundle_and_citation_revisions(self) -> None:
        self.provision()
        image = "ghcr.io/comses/catalog@sha256:" + "a" * 64
        revision = "b" * 40

        def archive(_revision: str, release_dir: Path) -> str:
            release_dir.mkdir(parents=True)
            return catalogctl.sha256_tree(release_dir)

        def render(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["bash", "scripts/compose.sh"]:
                Path(command[-1]).write_text("name: catalog\nservices: {}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(self.controller, "host_check"),
            mock.patch.object(self.controller, "require_clean_revision", return_value="c" * 40),
            mock.patch.object(
                self.controller,
                "pull_and_verify_image",
                return_value={"org.comses.catalog.citation-revision": "c" * 40},
            ),
            mock.patch.object(self.controller, "archive_release", side_effect=archive),
            mock.patch.object(self.controller.runner, "run", side_effect=render),
            mock.patch.object(self.controller, "compose"),
        ):
            self.controller.candidate_create(image, revision)

        candidate = catalogctl.require_json(self.layout.candidate)
        self.assertEqual(candidate["image"], image)
        self.assertEqual(candidate["bundle_revision"], revision)
        self.assertEqual(candidate["citation_revision"], "c" * 40)
        self.assertEqual(candidate["restore_status"], "pending")

    def test_candidate_requires_the_citation_checkout_from_the_catalog_gitlink(self) -> None:
        revision = "b" * 40
        expected_citation = "c" * 40
        with (
            mock.patch.object(
                self.controller,
                "command_output",
                side_effect=[revision, "", "", expected_citation, "d" * 40],
            ),
            self.assertRaisesRegex(catalogctl.CatalogError, "does not match the Catalog gitlink"),
        ):
            self.controller.require_clean_revision(revision)

    def test_candidate_rejects_an_image_built_with_another_citation_revision(self) -> None:
        image = "ghcr.io/comses/catalog@sha256:" + "a" * 64
        labels = {
            "org.opencontainers.image.revision": "b" * 40,
            "org.comses.catalog.citation-revision": "d" * 40,
        }
        with (
            mock.patch.object(self.controller.runner, "run"),
            mock.patch.object(self.controller, "command_output", return_value=json.dumps(labels)),
            self.assertRaisesRegex(catalogctl.CatalogError, "does not match the Catalog gitlink"),
        ):
            self.controller.pull_and_verify_image(image, "b" * 40, "c" * 40)

    def test_locked_candidate_cannot_be_replaced(self) -> None:
        self.provision()
        self.write_candidate(migration_status="failed")
        with (
            mock.patch.object(self.controller, "host_check"),
            mock.patch.object(self.controller, "require_clean_revision", return_value="c" * 40),
            mock.patch.object(
                self.controller,
                "pull_and_verify_image",
                return_value={"org.comses.catalog.citation-revision": "c" * 40},
            ),
            self.assertRaisesRegex(catalogctl.CatalogError, "locked"),
        ):
            self.controller.candidate_create(
                "ghcr.io/comses/catalog@sha256:" + "d" * 64,
                "e" * 40,
            )

    def test_restored_candidate_cannot_be_silently_replaced(self) -> None:
        self.provision()
        self.write_candidate(restore_status="succeeded")
        with (
            mock.patch.object(self.controller, "host_check"),
            mock.patch.object(self.controller, "require_clean_revision", return_value="c" * 40),
            mock.patch.object(
                self.controller,
                "pull_and_verify_image",
                return_value={"org.comses.catalog.citation-revision": "c" * 40},
            ),
            self.assertRaisesRegex(catalogctl.CatalogError, "locked by restore"),
        ):
            self.controller.candidate_create(
                "ghcr.io/comses/catalog@sha256:" + "d" * 64,
                "e" * 40,
            )

    def test_candidate_rejects_supporting_image_drift_on_an_active_host(self) -> None:
        self.provision()
        active = self.write_candidate()
        self.write_state(self.layout.active, **{key: value for key, value in active.items() if key != "schema_version"})
        self.layout.candidate.unlink()
        image = "ghcr.io/comses/catalog@sha256:" + "d" * 64

        def archive(_revision: str, release_dir: Path) -> str:
            release_dir.mkdir(parents=True)
            return catalogctl.sha256_tree(release_dir)

        def render(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["bash", "scripts/compose.sh"]:
                Path(command[-1]).write_text("name: catalog\nservices: {}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        def compose(_path: Path, *arguments: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = f"{image}\nnginx@changed\n" if arguments == ("config", "--images") else ""
            return subprocess.CompletedProcess(list(arguments), 0, output, "")

        with (
            mock.patch.object(self.controller, "host_check"),
            mock.patch.object(self.controller, "require_clean_revision", return_value="c" * 40),
            mock.patch.object(
                self.controller,
                "pull_and_verify_image",
                return_value={"org.comses.catalog.citation-revision": "c" * 40},
            ),
            mock.patch.object(self.controller, "archive_release", side_effect=archive),
            mock.patch.object(self.controller.runner, "run", side_effect=render),
            mock.patch.object(self.controller, "compose", side_effect=compose),
            self.assertRaisesRegex(catalogctl.CatalogError, "supporting service image digests changed"),
        ):
            self.controller.candidate_create(image, "e" * 40)

    def test_backup_writes_verified_artifact_checksum_and_receipt(self) -> None:
        self.provision()
        release = self.write_candidate()
        self.write_state(
            self.layout.active,
            host_id="staging",
            release_id="release-1",
            image="ghcr.io/comses/catalog@sha256:" + "a" * 64,
            compose_file=release["compose_file"],
            compose_sha256=release["compose_sha256"],
            release_dir=release["release_dir"],
            bundle_sha256=release["bundle_sha256"],
        )
        self.layout.canonical_compose.parent.mkdir(parents=True, exist_ok=True)
        self.layout.canonical_compose.write_bytes(Path(str(release["compose_file"])).read_bytes())

        def write_dump(_command: list[str], destination: BinaryIO, **_kwargs: object) -> None:
            destination.write(b"valid custom dump")

        with (
            mock.patch.object(self.controller, "db_identity", return_value=("catalog", "comses_catalog")),
            mock.patch.object(
                self.controller,
                "database_counts",
                return_value={"citation_publication": 10, "primary_publication": 3},
            ),
            mock.patch.object(
                self.controller,
                "postgres_versions",
                return_value={"server": "18.0", "pg_dump": "18.0", "pg_restore": "18.0"},
            ),
            mock.patch.object(self.controller.runner, "to_file", side_effect=write_dump),
            mock.patch.object(self.controller.runner, "from_file") as validate_dump,
        ):
            artifact = self.controller.backup()

        self.assertEqual(artifact.read_bytes(), b"valid custom dump")
        receipt_path = next((self.layout.receipts / "backups").glob("*.json"))
        receipt = catalogctl.require_json(receipt_path)
        self.assertEqual(receipt["sha256"], catalogctl.sha256_file(artifact))
        self.assertEqual(receipt["counts"]["citation_publication"], 10)
        self.assertIn(receipt["sha256"], artifact.with_suffix(".dump.sha256").read_text())
        self.assertTrue(validate_dump.call_args.kwargs["discard_stdout"])

    def test_plain_restore_rejects_database_level_commands_before_starting_services(self) -> None:
        dump = Path(self.temporary.name) / "unsafe.sql"
        dump.write_text("CREATE DATABASE other;\n", encoding="utf-8")
        with (
            mock.patch.object(self.controller, "compose") as compose,
            self.assertRaisesRegex(catalogctl.CatalogError, "database-level commands"),
        ):
            self.controller.restore(dump, "comses_catalog")
        compose.assert_not_called()

    def test_restore_rejects_a_mismatched_adjacent_checksum(self) -> None:
        dump = Path(self.temporary.name) / "catalog.sql"
        dump.write_text("SELECT 1;\n", encoding="utf-8")
        dump.with_suffix(".sql.sha256").write_text(f"{'0' * 64}  {dump.name}\n", encoding="utf-8")
        with (
            mock.patch.object(self.controller, "compose") as compose,
            self.assertRaisesRegex(catalogctl.CatalogError, "checksum does not match"),
        ):
            self.controller.restore(dump, "comses_catalog")
        compose.assert_not_called()

    def test_schema_migration_requires_confirmation_and_runs_ordered_guards(self) -> None:
        self.provision()
        self.write_candidate()
        with self.assertRaisesRegex(catalogctl.CatalogError, "CONFIRM_SCHEMA_MIGRATION"):
            self.controller.schema_migrate(False)

        calls: list[tuple[str, ...]] = []

        def manage(_compose: Path, *arguments: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            output = "planned migration\n" if arguments == ("migrate", "--plan") else ""
            return subprocess.CompletedProcess(list(arguments), 0, output, "")

        with (
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "manage", side_effect=manage),
        ):
            self.controller.schema_migrate(True)

        self.assertEqual(
            calls,
            [
                ("makemigrations", "--check", "--dry-run"),
                ("migrate", "--plan"),
                ("migrate", "--noinput"),
                ("migrate", "--check"),
            ],
        )
        candidate = catalogctl.require_json(self.layout.candidate)
        self.assertEqual(candidate["migration_status"], "succeeded")
        self.assertEqual(candidate["migration_plan"], ["planned migration"])

    def test_data_rebuild_validates_search_and_records_new_aliases(self) -> None:
        self.provision()
        self.write_candidate(migration_status="succeeded")
        old_aliases = {"publication": "publication-old"}
        new_aliases = {"publication": "publication-new"}
        with (
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "manage") as manage,
            mock.patch.object(self.controller, "alias_targets", side_effect=[old_aliases, new_aliases]),
        ):
            self.controller.data_rebuild()

        self.assertEqual(
            [call.args[1:] for call in manage.call_args_list],
            [
                ("rebuild_es_index",),
                ("validate_search_indexes",),
                ("populate_visualization_cache", "--clear"),
            ],
        )
        candidate = catalogctl.require_json(self.layout.candidate)
        self.assertEqual(candidate["data_rebuild_status"], "succeeded")
        self.assertEqual(candidate["search_aliases_before"], old_aliases)
        self.assertEqual(candidate["search_aliases_after"], new_aliases)

    def test_alias_snapshot_includes_model_documentation(self) -> None:
        response = {
            "model_documentation-20260920t120000z": {
                "aliases": {"model_documentation": {}}
            },
            "unmanaged-index": {"aliases": {"unmanaged": {}}},
        }
        with mock.patch.object(
            self.controller,
            "compose",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ""),
        ):
            aliases = self.controller.alias_targets(Path("docker-compose.yml"))

        self.assertEqual(
            aliases,
            {"model_documentation": "model_documentation-20260920t120000z"},
        )

    def test_candidate_retry_requires_confirmation_and_only_resets_failed_stage(self) -> None:
        self.provision()
        self.write_candidate(
            migration_status="succeeded",
            data_rebuild_status="failed",
            data_rebuild_failed_at=catalogctl.utc_now(),
            data_rebuild_error="index failed",
            search_aliases_before={"publication": "publication-old"},
        )
        with self.assertRaisesRegex(catalogctl.CatalogError, "CONFIRM_CANDIDATE_RETRY"):
            self.controller.candidate_retry(False)

        with (
            mock.patch.object(self.controller, "set_alias_targets") as restore_aliases,
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-old"},
            ),
        ):
            self.controller.candidate_retry(True)

        candidate = catalogctl.require_json(self.layout.candidate)
        self.assertEqual(candidate["migration_status"], "succeeded")
        self.assertEqual(candidate["data_rebuild_status"], "pending")
        self.assertNotIn("data_rebuild_error", candidate)
        restore_aliases.assert_called_once()
        history = json.loads(self.layout.history.read_text().strip())
        self.assertEqual(history["operation"], "candidate-retry")
        self.assertEqual(history["stage"], "data-rebuild")

    def test_candidate_retry_can_unlock_an_interrupted_restore(self) -> None:
        self.provision()
        self.write_candidate(
            restore_status="in_progress",
            restore_started_at=catalogctl.utc_now(),
        )

        self.controller.candidate_retry(True)

        candidate = catalogctl.require_json(self.layout.candidate)
        self.assertEqual(candidate["restore_status"], "pending")
        history = json.loads(self.layout.history.read_text().strip())
        self.assertEqual(history["stage"], "restore")

    def test_successful_deploy_publishes_active_state_after_health_check(self) -> None:
        self.provision()
        candidate = self.write_candidate(
            migration_status="succeeded",
            data_rebuild_status="succeeded",
            search_aliases_after={"publication": "publication-new"},
        )
        events: list[str] = []

        def verify(_compose: Path) -> None:
            events.append("healthy")

        def publish(source: Path) -> None:
            self.assertEqual(events, ["healthy"])
            events.append("published")
            catalogctl.atomic_write(self.layout.canonical_compose, source.read_bytes())

        with (
            mock.patch.object(self.controller, "manage"),
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-new"},
            ),
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "verify_runtime", side_effect=verify),
            mock.patch.object(self.controller, "publish_compose", side_effect=publish),
        ):
            self.controller.deploy()

        active = catalogctl.require_json(self.layout.active)
        self.assertEqual(events, ["healthy", "published"])
        self.assertEqual(active["image"], candidate["image"])
        self.assertEqual(active["search_aliases"], {"publication": "publication-new"})
        self.assertFalse(self.layout.candidate.exists())
        self.assertFalse(self.layout.journal.exists())
        history = json.loads(self.layout.history.read_text().strip())
        self.assertEqual(history["result"], "succeeded")

    def test_deploy_rejects_search_alias_drift_before_startup(self) -> None:
        self.provision()
        self.write_candidate(
            migration_status="succeeded",
            data_rebuild_status="succeeded",
            search_aliases_after={"publication": "publication-new"},
        )

        with (
            mock.patch.object(self.controller, "manage"),
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-old"},
            ),
            mock.patch.object(self.controller, "compose") as compose,
            self.assertRaisesRegex(catalogctl.CatalogError, "live search aliases differ"),
        ):
            self.controller.deploy()

        compose.assert_not_called()
        self.assertTrue(self.layout.candidate.exists())
        self.assertFalse(self.layout.journal.exists())

    def test_failed_deploy_recovers_previous_runtime_and_retains_candidate(self) -> None:
        self.provision()
        self.write_candidate(migration_status="succeeded", data_rebuild_status="succeeded")
        old_compose = self.layout.releases / "old" / "docker-compose.yml"
        old_compose.parent.mkdir(parents=True)
        old_compose.write_text("name: catalog\nservices: {}\n", encoding="utf-8")
        previous = self.write_state(
            self.layout.active,
            host_id="staging",
            release_id="old-release",
            image="ghcr.io/comses/catalog@sha256:" + "f" * 64,
            compose_file=str(old_compose),
            compose_sha256=catalogctl.sha256_file(old_compose),
            release_dir=str(old_compose.parent),
            bundle_sha256=catalogctl.sha256_tree(old_compose.parent, excluded={"docker-compose.yml"}),
            search_aliases={},
        )
        self.write_state(self.layout.rollback, release_id="older-release")

        with (
            mock.patch.object(self.controller, "manage"),
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-new"},
            ),
            mock.patch.object(self.controller, "compose", side_effect=catalogctl.CatalogError("startup failed")),
            mock.patch.object(self.controller, "restore_runtime", return_value=True) as restore,
            self.assertRaisesRegex(catalogctl.CatalogError, "prior runtime recovered=True"),
        ):
            self.controller.deploy()

        restore.assert_called_once()
        self.assertEqual(catalogctl.require_json(self.layout.active), previous)
        self.assertTrue(self.layout.candidate.exists())
        self.assertFalse(self.layout.journal.exists())
        history = json.loads(self.layout.history.read_text().strip())
        self.assertEqual(history["result"], "failed-recovered")

    def test_deploy_failure_after_state_publication_restores_previous_state(self) -> None:
        self.provision()
        self.write_candidate(migration_status="succeeded", data_rebuild_status="succeeded")
        old_compose = self.layout.releases / "old" / "docker-compose.yml"
        old_compose.parent.mkdir(parents=True)
        old_compose.write_text("name: catalog\nservices: {}\n", encoding="utf-8")
        previous = self.write_state(
            self.layout.active,
            host_id="staging",
            release_id="old-release",
            image="ghcr.io/comses/catalog@sha256:" + "f" * 64,
            compose_file=str(old_compose),
            compose_sha256=catalogctl.sha256_file(old_compose),
            release_dir=str(old_compose.parent),
            bundle_sha256=catalogctl.sha256_tree(old_compose.parent, excluded={"docker-compose.yml"}),
            search_aliases={},
        )
        previous_rollback = self.write_state(self.layout.rollback, release_id="older-release")
        update_journal = self.controller.update_journal

        def fail_commit(
            journal: dict[str, object], checkpoint: str, **values: object
        ) -> dict[str, object]:
            if checkpoint == "committed":
                raise OSError("state disk full")
            return update_journal(journal, checkpoint, **values)

        with (
            mock.patch.object(self.controller, "manage"),
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-new"},
            ),
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "verify_runtime"),
            mock.patch.object(self.controller, "publish_compose"),
            mock.patch.object(self.controller, "restore_runtime", return_value=True),
            mock.patch.object(self.controller, "update_journal", side_effect=fail_commit),
            self.assertRaisesRegex(catalogctl.CatalogError, "prior runtime recovered=True"),
        ):
            self.controller.deploy()

        self.assertEqual(catalogctl.require_json(self.layout.active), previous)
        self.assertEqual(catalogctl.require_json(self.layout.rollback), previous_rollback)
        self.assertTrue(self.layout.candidate.exists())
        self.assertFalse(self.layout.journal.exists())

    def test_recover_finalizes_a_committed_deploy_once(self) -> None:
        self.provision()
        candidate = self.write_candidate(
            migration_status="succeeded",
            data_rebuild_status="succeeded",
            search_aliases_after={"publication": "publication-new"},
        )

        with (
            mock.patch.object(self.controller, "manage"),
            mock.patch.object(
                self.controller,
                "alias_targets",
                return_value={"publication": "publication-new"},
            ),
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "verify_runtime"),
            mock.patch.object(self.controller, "publish_compose"),
            mock.patch.object(self.controller, "append_history_once", side_effect=OSError("history disk full")),
            self.assertRaisesRegex(catalogctl.CatalogError, "committed but finalization failed"),
        ):
            self.controller.deploy()

        active = catalogctl.require_json(self.layout.active)
        self.assertEqual(active["image"], candidate["image"])
        self.assertEqual(catalogctl.require_json(self.layout.journal)["checkpoint"], "committed")
        self.assertTrue(self.layout.candidate.exists())

        with (
            mock.patch.object(self.controller, "compose"),
            mock.patch.object(self.controller, "set_alias_targets"),
            mock.patch.object(self.controller, "verify_runtime"),
        ):
            self.controller.recover()

        self.assertFalse(self.layout.candidate.exists())
        self.assertFalse(self.layout.journal.exists())
        history = [json.loads(line) for line in self.layout.history.read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["operation"], "deploy")
        self.assertEqual(history[0]["result"], "succeeded")


if __name__ == "__main__":
    unittest.main()
