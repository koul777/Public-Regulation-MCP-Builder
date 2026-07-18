import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeploymentDefaultsTests(unittest.TestCase):
    def test_env_example_defaults_to_secure_runtime_mode(self):
        env_values = self._read_env_example()

        self.assertEqual("production", env_values.get("APP_ENV"))
        self.assertEqual("true", env_values.get("API_AUTH_REQUIRED"))
        self.assertEqual("true", env_values.get("TENANT_STORAGE_ISOLATION"))
        self.assertEqual("local", env_values.get("STREAMLIT_APP_ENV"))
        self.assertEqual("false", env_values.get("STREAMLIT_API_AUTH_REQUIRED"))
        self.assertEqual("false", env_values.get("STREAMLIT_TENANT_STORAGE_ISOLATION"))

    def test_docker_compose_uses_dotenv_not_example_file(self):
        compose_text = self._read_docker_compose()
        stripped_lines = [line.strip() for line in compose_text.splitlines()]
        env_file_lines = [
            index for index, line in enumerate(stripped_lines) if line == "env_file:"
        ]

        self.assertNotIn(".env.example", compose_text)
        self.assertGreater(env_file_lines, [], "docker-compose.yml must declare env_file")
        for index in env_file_lines:
            self.assertLess(index + 1, len(stripped_lines))
            self.assertEqual("- .env", stripped_lines[index + 1])

    def test_docker_compose_passes_supported_api_auth_sources(self):
        compose_text = self._read_docker_compose()

        self.assertIn("API_AUTH_TOKEN: ${API_AUTH_TOKEN:-}", compose_text)
        self.assertIn("API_AUTH_TOKENS: ${API_AUTH_TOKENS:-}", compose_text)

    def test_api_service_declares_ready_healthcheck(self):
        compose_text = self._read_docker_compose()

        self.assertIn("healthcheck:", compose_text)
        self.assertIn("http://127.0.0.1:8000/ready", compose_text)

    def test_shared_deployment_overlay_requires_production_auth_and_isolation(self):
        overlay = (REPO_ROOT / "docker-compose.shared.yml").read_text(encoding="utf-8")

        self.assertIn("env_file:", overlay)
        self.assertIn("- .env.shared", overlay)
        self.assertIn('APP_ENV: production', overlay)
        self.assertIn('API_AUTH_REQUIRED: "true"', overlay)
        self.assertIn('API_AUDIT_ENABLED: "true"', overlay)
        self.assertIn('TENANT_STORAGE_ISOLATION: "true"', overlay)
        self.assertIn("API_AUTH_TOKEN:?Set API_AUTH_TOKEN", overlay)

    def test_shared_env_example_is_secretless_and_protected(self):
        shared = (REPO_ROOT / ".env.shared.example").read_text(encoding="utf-8")

        self.assertIn("APP_ENV=production", shared)
        self.assertIn("API_AUTH_REQUIRED=true", shared)
        self.assertIn("API_AUDIT_ENABLED=true", shared)
        self.assertIn("TENANT_STORAGE_ISOLATION=true", shared)
        self.assertIn("API_AUTH_TOKEN=", shared)
        self.assertIn("Inject API_AUTH_TOKEN at deployment time", shared)

    def test_only_the_reviewed_auto_release_workflow_is_tracked(self):
        try:
            result = subprocess.run(
                ["git", "ls-files", "--stage", "--", ".github/workflows"],
                cwd=REPO_ROOT,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            self.skipTest("git executable is required to inspect the release index")
        except subprocess.CalledProcessError as exc:
            self.skipTest(
                "git metadata is unavailable to inspect the release index: "
                f"{exc.stderr.strip()}"
            )

        tracked_paths = [
            line.rsplit("\t", 1)[-1]
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        self.assertEqual([".github/workflows/auto-release.yml"], tracked_paths)

    def _read_env_example(self):
        env_path = REPO_ROOT / ".env.example"
        env_values = {}
        for line_number, raw_line in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertIn("=", line, f"Malformed .env.example line {line_number}")
            key, value = line.split("=", 1)
            env_values[key] = value
        return env_values

    def _read_docker_compose(self):
        return (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
