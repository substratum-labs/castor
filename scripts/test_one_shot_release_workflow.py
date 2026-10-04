"""Static release gate checks that run without registry credentials."""

from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/one-shot-release.yml"


class OneShotReleaseWorkflowTests(unittest.TestCase):
    def test_publication_requires_tag_protected_environment_and_arming_secret(self):
        source = WORKFLOW.read_text()
        self.assertIn("tags: ['one-shot-v*']", source)
        self.assertNotIn("pull_request:", source)
        self.assertIn("environment: castor-one-shot-release", source)
        self.assertIn("secrets.CASTOR_RELEASE_ARMED", source)
        self.assertLess(source.index("CASTOR_RELEASE_ARMED"), source.index("docker push"))

    def test_published_subjects_are_attested_and_verified_before_release(self):
        source = WORKFLOW.read_text()
        self.assertIn("actions/attest@v4", source)
        self.assertIn("subject-path:", source)
        self.assertIn("subject-name:", source)
        self.assertIn("subject-digest:", source)
        self.assertIn("gh attestation verify", source)
        self.assertLess(source.index("gh attestation verify"), source.index("gh release create"))
        self.assertIn("--source-digest", source)
        self.assertIn("export DOCKER_CONFIG=$(mktemp -d)", source)
        self.assertNotIn("gh release edit", source)


if __name__ == "__main__":
    unittest.main()
