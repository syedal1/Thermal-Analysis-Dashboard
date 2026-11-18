"""Offline PowerPoint API contracts: synthetic snapshots, no renderer or DuckDB."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import shared_dashboard as shared
from powerpoint_export import PowerPointExportError


class SharedPowerPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository"
        self.publish = Path(temporary.name) / "published"
        self.slug = "WW46-alice"
        self.version = "ab" * 16
        self.state = self.root / "Saved_Sessions" / "profiles" / self.slug
        self.tree = self.state / "versions" / self.version
        self.manifest = self.state / "manifest.json"
        self.profile_path = self.root / "Profiles" / f"{self.slug}.json"
        self.profile = {"owner": "Edited owner", "work_week": "WW99", "lots": ["L1"],
                        "source": "manual", "data_domain": "CLASS", "slug": self.slug}
        self.snapshot = {
            "version": self.version, "data_domain": "SORT",
            "profile": {**self.profile, "owner": "Alice", "work_week": "WW46", "data_domain": "SORT"},
            "reports": {category: f"{category}/dashboard.html" for category in shared.CATEGORIES},
            "counts": shared._zero_counts(),
            "category_counts": {category: shared._zero_counts() for category in shared.CATEGORIES},
            "category_tests": {category: [category + "_test"] for category in shared.CATEGORIES},
            "config_snapshot": {},
        }
        shared._atomic_json(self.profile_path, self.profile)
        for relative in self.snapshot["reports"].values():
            shared._atomic_bytes(self.tree / relative, b'<!doctype html><script class="plot-data" '
                                 b'type="application/json">{"data":[]}</script>')
        self.save_snapshot()
        shared._atomic_json(self.manifest, {"version": self.version})
        shared._atomic_bytes(self.root / "assets" / "profile_portal.html",
                             b'<script id="portal-data" type="application/json">__PORTAL_DATA__</script>')

        # The library's unrelated connector status imports the data store. Stub it
        # so these tests exercise real metadata/publication without loading DuckDB.
        connector = SimpleNamespace(PARAMETER_FILES={}, parameter_definition=None, load_settings=None)
        self.enterContext(patch.dict(sys.modules, {"aqua_connector": connector}))
        self.exporter = self.enterContext(patch("powerpoint_export.export_report_pptx", side_effect=self.fake_export))
        self.no_processing = [self.enterContext(patch.object(
            shared, name, side_effect=AssertionError("PowerPoint must not process/pull/query")))
            for name in ("_process", "_process_files", "_source_fingerprints", "export_dashboard")]
        self.app = shared.create_app(self.root, self.publish)
        self.app.testing = True
        self.client = self.app.test_client()
        self.url = f"/api/profiles/{self.slug}/powerpoint"

    def save_snapshot(self):
        shared._atomic_json(self.tree / "snapshot.json", self.snapshot)

    def fake_export(self, report_path, destination=None, title=None, template_path=None):
        self.assertTrue(self.root.joinpath(".shared_dashboard.lock").is_file())
        self.assertTrue(self.publish.joinpath(".shared_dashboard.lock").is_file())
        self.assertTrue(Path(report_path).is_file())
        self.assertFalse(Path(destination).is_relative_to(self.tree))
        Path(destination).write_bytes(b"mock-pptx:" + Path(report_path).parent.name.encode())
        return Path(destination)

    def body(self, **changes):
        return {"category": "thermal_diode", "version": self.version, "data_domain": "SORT", **changes}

    def generate(self, **changes):
        return self.client.post(self.url, json=self.body(**changes))

    def deck(self, category="thermal_diode", version=None):
        return self.state / "exports" / (version or self.version) / f"{category}.pptx"

    def download_url(self, category="thermal_diode", version=None):
        return f"/exports/{self.slug}/{version or self.version}/{category}.pptx"

    def metadata(self):
        response = self.client.get(f"/api/profiles/{self.slug}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def frozen_files(self):
        paths = [self.manifest, self.profile_path, *self.tree.rglob("*")]
        return {str(path): path.read_bytes() for path in paths if path.is_file()}

    def static_data(self):
        html = (self.publish / "index.html").read_text(encoding="utf-8")
        return json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])

    def assert_no_partial_exports(self):
        folder = self.state / "exports"
        self.assertFalse([path for path in folder.rglob("*") if path.is_file() or path.name.startswith(".")])

    def test_three_categories_stable_cache_titles_and_snapshot_immutability(self):
        before = self.frozen_files()
        self.assertEqual(self.metadata()["powerpoints"], dict.fromkeys(shared.CATEGORIES))
        for category in shared.CATEGORIES:
            with self.subTest(category=category):
                first = self.generate(category=category)
                self.assertEqual(first.status_code, 200, first.get_json())
                self.assertEqual(first.get_json(), {"url": self.download_url(category)})
                call = self.exporter.call_args
                self.assertEqual(call.args, (self.tree / self.snapshot["reports"][category],))
                self.assertEqual(call.kwargs["title"], f"{shared.CATEGORY_TITLES[category]} | WW46 | Alice | SORT")
                deck = self.deck(category)
                content, mtime = deck.read_bytes(), deck.stat().st_mtime_ns
                count = self.exporter.call_count
                self.assertEqual(self.generate(category=category).get_json(), first.get_json())
                self.assertEqual(self.exporter.call_count, count)
                self.assertEqual((deck.read_bytes(), deck.stat().st_mtime_ns), (content, mtime))
                self.assertEqual((self.publish / self.download_url(category).lstrip("/")).read_bytes(), content)
        self.assertEqual(self.exporter.call_count, 3)
        self.assertEqual(self.frozen_files(), before)
        for guard in self.no_processing:
            guard.assert_not_called()
        self.assertEqual(self.metadata()["powerpoints"], {c: self.download_url(c) for c in shared.CATEGORIES})

    def test_class_snapshot_supported(self):
        self.snapshot["data_domain"] = "CLASS"
        self.snapshot["profile"]["data_domain"] = "CLASS"
        self.save_snapshot()
        self.assertEqual(self.generate(data_domain="CLASS").status_code, 200)
        self.assertTrue(self.exporter.call_args.kwargs["title"].endswith(" | CLASS"))

    def test_configured_powerpoint_template_is_passed_to_exporter(self):
        template = self.root / "config" / "PPT_Template.pptx"
        shared._atomic_bytes(template, b"protected template")
        self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(self.exporter.call_args.kwargs["template_path"], template)
        self.assertEqual(template.read_bytes(), b"protected template")

    def test_stale_version_or_domain_even_when_cached(self):
        self.assertEqual(self.generate().status_code, 200)
        before = self.frozen_files()
        self.exporter.reset_mock()
        for changes in ({"version": "cd" * 16}, {"data_domain": "CLASS"}):
            response = self.generate(**changes)
            self.assertEqual(response.status_code, 409)
            self.assertIn("reload", response.get_json()["error"])
        self.exporter.assert_not_called()
        self.assertEqual(self.frozen_files(), before)

    def test_snapshot_read_under_existing_lock(self):
        original = shared._snapshot

        def checked(*args):
            self.assertTrue((self.root / ".shared_dashboard.lock").exists())
            self.assertTrue((self.publish / ".shared_dashboard.lock").exists())
            return original(*args)

        with patch.object(shared, "_snapshot", side_effect=checked):
            self.assertEqual(self.generate().status_code, 200)
        with shared._locked(self.root, self.publish):
            self.assertEqual(self.generate().status_code, 409)
        self.assertFalse((self.root / ".shared_dashboard.lock").exists())
        self.assertFalse((self.publish / ".shared_dashboard.lock").exists())

    def test_no_snapshot_and_missing_profile(self):
        self.manifest.unlink()
        self.assertEqual(self.generate().status_code, 409)
        self.assertEqual(self.metadata()["powerpoints"], dict.fromkeys(shared.CATEGORIES))
        self.assertEqual(self.client.post("/api/profiles/absent/powerpoint", json=self.body()).status_code, 404)
        self.exporter.assert_not_called()

    def test_exact_request_fields_and_strict_types(self):
        invalid = [{}, {**self.body(), "extra": True}]
        invalid += [{key: value for key, value in self.body().items() if key != missing}
                    for missing in self.body()]
        for field in self.body():
            invalid += [self.body(**{field: value}) for value in (None, True, 1, [], {}, "")]
        invalid += [self.body(category=value) for value in ("Thermal Diode", "DTS", "../dts", "dts.pptx")]
        invalid += [self.body(version=value) for value in
                    (self.version.upper(), "ab" * 15, "ab" * 17, "g" * 32, "../" + self.version, self.version + "\n")]
        invalid += [self.body(data_domain=value) for value in ("sort", " CLASS ", "TRACE")]
        for body in invalid:
            with self.subTest(body=body):
                response = self.client.post(self.url, json=body)
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(set(response.get_json()), {"error"})
        self.exporter.assert_not_called()
        self.assertFalse((self.state / "exports").exists())

    def test_json_parser_and_mimetype_guard(self):
        for raw in ('null', '[]', 'true', '{bad', '{"category":NaN}',
                    '{"category":"dts","category":"dtsmon"}'):
            self.assertEqual(self.client.post(self.url, data=raw, content_type="application/json").status_code, 400)
        for mimetype in ("text/plain", "application/x-www-form-urlencoded", "application/other+json"):
            self.assertEqual(self.client.post(self.url, data=json.dumps(self.body()), content_type=mimetype).status_code, 415)
        self.assertEqual(self.client.post(self.url, data=" " * (shared.MAX_BODY + 1),
                                         content_type="application/json").status_code, 413)
        self.exporter.assert_not_called()

    def test_origin_guard(self):
        for headers in ({"Origin": "null"}, {"Origin": "https://evil.example"},
                        {"Origin": "http://localhost:99"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost"}):
            self.assertEqual(self.client.post(self.url, json=self.body(), headers=headers).status_code, 403)
        self.exporter.assert_not_called()
        response = self.client.post(self.url, json=self.body(), headers={
            "Origin": "http://localhost:80", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_category_or_source(self):
        for missing in (None, "dts/absent.html"):
            self.snapshot["reports"]["dts"] = missing
            self.save_snapshot()
            self.assertEqual(self.generate(category="dts").status_code, 404)
        del self.snapshot["reports"]["dts"]
        self.save_snapshot()
        self.assertEqual(self.generate(category="dts").status_code, 404)
        self.exporter.assert_not_called()

    def test_unsafe_snapshot_report_paths(self):
        for relative in ([], {}, 7, True, "", "../outside.html", "/outside.html", "C:/outside.html",
                         "dts\\dashboard.html", "dts/../dts/dashboard.html", "dts/./dashboard.html",
                         "dts//dashboard.html", ".hidden.html", "dts/.hidden.html", "snapshot.json"):
            with self.subTest(relative=relative):
                self.snapshot["reports"]["dts"] = relative
                self.save_snapshot()
                self.assertEqual(self.generate(category="dts").status_code, 422)
        self.exporter.assert_not_called()

    def test_dependency_and_rendering_errors_preserve_all_committed_files(self):
        shared.publish_portal(self.root, self.publish)
        before = self.frozen_files()
        published = {str(p): p.read_bytes() for p in self.publish.rglob("*") if p.is_file()}
        for message in ("PowerPoint export requires python-pptx.", "PNG rendering failed: Kaleido/Chrome unavailable."):
            def fail(source, destination=None, title=None):
                Path(destination).write_bytes(b"partial deck")
                raise PowerPointExportError(message)

            self.exporter.side_effect = fail
            response = self.generate()
            self.assertEqual(response.status_code, 503)
            self.assertIn(message, response.get_json()["error"])
            self.assertIn("retry", response.get_json()["error"])
            self.assertEqual(self.frozen_files(), before)
            self.assertEqual({str(p): p.read_bytes() for p in self.publish.rglob("*") if p.is_file()}, published)
            self.assert_no_partial_exports()
            self.assertIsNone(self.metadata()["powerpoints"]["thermal_diode"])

    def test_exporter_must_produce_file(self):
        self.exporter.side_effect = None
        self.exporter.return_value = self.tree / "dts" / "dashboard.html"
        self.assertEqual(self.generate().status_code, 503)
        self.assert_no_partial_exports()

    def test_unexpected_failure_and_atomic_cache_commit_failure(self):
        before = self.frozen_files()
        self.exporter.side_effect = OSError("disk full")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.assertEqual(self.generate().status_code, 500)
        self.assert_no_partial_exports()
        self.exporter.side_effect = self.fake_export
        with patch.object(shared.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs(self.app.logger, level="ERROR"):
            self.assertEqual(self.generate().status_code, 500)
        self.assert_no_partial_exports()
        self.assertEqual(self.frozen_files(), before)

    def test_static_relative_links_copied_before_index_commit(self):
        for category in ("thermal_diode", "dtsmon"):
            self.deck(category).parent.mkdir(parents=True, exist_ok=True)
            self.deck(category).write_bytes(b"existing mock deck")
        abandoned = self.deck("dts", version="cd" * 16)
        abandoned.parent.mkdir(parents=True)
        abandoned.write_bytes(b"uncommitted deck")
        original = shared._atomic_bytes

        def checked(path, content):
            if path == self.publish / "index.html":
                for category in ("thermal_diode", "dtsmon"):
                    self.assertEqual((self.publish / self.download_url(category).lstrip("/")).read_bytes(), b"existing mock deck")
            return original(path, content)

        before = self.frozen_files()
        with patch.object(shared, "_atomic_bytes", side_effect=checked):
            shared.publish_portal(self.root, self.publish)
        profile = self.static_data()["profiles"][0]
        self.assertEqual(profile["powerpoints"], {"thermal_diode": self.download_url().lstrip("/"),
                         "dts": None, "dtsmon": self.download_url("dtsmon").lstrip("/")})
        self.assertTrue(all(url.startswith("reports/") for url in profile["reports"].values()))
        self.assertEqual(profile["snapshot"], self.snapshot)
        self.assertFalse((self.publish / "exports" / self.slug / ("cd" * 16)).exists())
        self.assertEqual(self.frozen_files(), before)
        self.exporter.assert_not_called()

    def test_failed_publish_leaves_local_cache_and_old_index_then_retries(self):
        shared.publish_portal(self.root, self.publish)
        previous_index = (self.publish / "index.html").read_bytes()

        def broken_copy(source, destination, *args, **kwargs):
            destination.write(b"partial copy")
            raise OSError("share unavailable")

        with patch.object(shared.shutil, "copyfileobj", side_effect=broken_copy):
            response = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()), {"url", "warning"})
        self.assertIn("POST /api/publish", response.get_json()["warning"])
        self.assertEqual((self.publish / "index.html").read_bytes(), previous_index)
        self.assertTrue(self.deck().is_file())
        self.assertFalse([p for p in (self.publish / "exports").rglob("*") if p.is_file()])
        download = self.client.get(response.get_json()["url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, self.deck().read_bytes())
        download.close()
        self.assertEqual(self.metadata()["powerpoints"]["thermal_diode"], self.download_url())
        self.assertEqual(self.generate().get_json(), {"url": self.download_url()})
        self.exporter.assert_called_once()
        self.assertEqual((self.publish / self.download_url().lstrip("/")).read_bytes(), self.deck().read_bytes())
        self.assertEqual(self.static_data()["profiles"][0]["powerpoints"]["thermal_diode"], self.download_url().lstrip("/"))

    def test_attachment_headers_and_missing_download(self):
        self.assertEqual(self.client.get(self.download_url()).status_code, 404)
        self.assertEqual(self.generate().status_code, 200)
        response = self.client.get(self.download_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, shared.POWERPOINT_MIME)
        self.assertEqual(response.headers["Content-Disposition"], "attachment; filename=thermal_diode.pptx")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.data, self.deck().read_bytes())
        response.close()

    def test_metadata_ignores_old_versions_but_old_downloads_remain_stable(self):
        self.assertEqual(self.generate().status_code, 200)
        old_url = self.download_url()
        new_version = "cd" * 16
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["version"] = new_version
        shared._atomic_json(self.state / "versions" / new_version / "snapshot.json", snapshot)
        shared._atomic_json(self.manifest, {"version": new_version})
        self.assertEqual(self.metadata()["powerpoints"], dict.fromkeys(shared.CATEGORIES))
        self.assertEqual(self.generate().status_code, 409)
        response = self.client.get(old_url)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_download_structure_dotfiles_and_traversal(self):
        self.assertEqual(self.generate().status_code, 200)
        invalid = [".hidden", f"{self.slug}/{self.version}/.dts.pptx",
                   f"{self.slug}/{self.version}/thermal_diode.pptx/extra",
                   f"{self.slug}/{self.version}/snapshot.json", f"{self.slug}/{self.version}/DTS.pptx",
                   f"{self.slug}/{self.version}/unknown.pptx", f"{self.slug}/{self.version.upper()}/dts.pptx",
                   f"{self.slug}/short/dts.pptx", f"absent/{self.version}/thermal_diode.pptx",
                   f"CON/{self.version}/dts.pptx", f".hidden/{self.version}/dts.pptx",
                   f"{self.slug}/{self.version}/%2e%2e/thermal_diode.pptx",
                   f"{self.slug}/{self.version}/dts%5cother.pptx",
                   f"{self.slug}/{self.version}/dts.pptx:stream", f"../{self.version}/dts.pptx"]
        for filename in invalid:
            with self.subTest(filename=filename):
                self.assertEqual(self.client.get("/exports/" + filename).status_code, 404)

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")

    def test_symlink_report_rejected_even_inside_snapshot(self):
        path = self.tree / self.snapshot["reports"]["dts"]
        path.unlink()
        self.symlink(path, self.tree / self.snapshot["reports"]["thermal_diode"])
        self.assertEqual(self.generate(category="dts").status_code, 422)
        self.exporter.assert_not_called()

    def test_symlink_export_rejected_in_metadata_and_download(self):
        self.deck().parent.mkdir(parents=True)
        self.symlink(self.deck(), self.tree / self.snapshot["reports"]["thermal_diode"])
        self.assertEqual(self.client.get(self.download_url()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/profiles/{self.slug}").status_code, 422)
        self.assertEqual(self.generate().status_code, 422)
        self.exporter.assert_not_called()

    def test_symlink_export_parent_outside_root_rejected(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.symlink(self.state / "exports", outside)
        self.assertEqual(self.client.get(self.download_url()).status_code, 404)
        self.assertEqual(self.generate().status_code, 400)
        self.assertEqual(list(outside.iterdir()), [])
        self.exporter.assert_not_called()

    def test_publish_symlink_cannot_overwrite_user_file(self):
        target = self.publish / self.download_url().lstrip("/")
        target.parent.mkdir(parents=True)
        other = self.publish / "user-file.pptx"
        other.write_bytes(b"user content")
        self.symlink(target, other)
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertIn("warning", response.get_json())
        self.assertEqual(other.read_bytes(), b"user content")
        self.assertTrue(self.deck().is_file())

    def test_mocked_symlink_guards_without_privileges(self):
        original = Path.is_symlink
        targets = [self.tree / self.snapshot["reports"]["thermal_diode"],
                   self.state / "exports", self.deck()]
        for target in targets:
            with self.subTest(target=target):
                def linked(path):
                    return path == target or original(path)

                with patch.object(Path, "is_symlink", linked):
                    self.assertEqual(self.generate().status_code, 422)
                    if target != targets[0]:
                        self.assertEqual(self.client.get(self.download_url()).status_code, 404)
        self.exporter.assert_not_called()

    def test_index_commit_failure_preserves_previous_index_and_cached_deck(self):
        shared.publish_portal(self.root, self.publish)
        previous_index = (self.publish / "index.html").read_bytes()
        before = self.frozen_files()
        replace = shared.os.replace

        def fail_index(source, destination):
            if Path(destination) == self.publish / "index.html":
                raise OSError("index is in use")
            return replace(source, destination)

        with patch.object(shared.os, "replace", side_effect=fail_index):
            response = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertIn("warning", response.get_json())
        self.assertEqual((self.publish / "index.html").read_bytes(), previous_index)
        self.assertEqual(self.frozen_files(), before)
        self.assertTrue(self.deck().is_file())
        self.assertEqual((self.publish / self.download_url().lstrip("/")).read_bytes(), self.deck().read_bytes())
        self.assertFalse(list(self.publish.glob(".*.tmp")))
        self.assertEqual(self.client.post("/api/publish", json={}).get_json(), {"published": True})
        self.exporter.assert_called_once()

    def test_windows_reparse_point_guard_without_symlink_privileges(self):
        original = Path.lstat

        def reparse(path, *args, **kwargs):
            if path == self.state:
                return SimpleNamespace(st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", reparse):
            # Test the reparse attribute guard independently of is_symlink's stat fields.
            with patch.object(Path, "is_symlink", return_value=False):
                with self.assertRaises(shared.SharedDashboardError):
                    shared._powerpoint_path(self.root, self.slug, self.version, "dts")


if __name__ == "__main__":
    unittest.main()