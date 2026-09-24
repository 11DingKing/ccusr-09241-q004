"""验证项目包能够在干净环境中载入。"""

import unittest

from telemedicine_continuity import PROJECT_CODE, project_info


class ProjectPackageTests(unittest.TestCase):
    def test_project_identity_is_stable(self) -> None:
        self.assertEqual(project_info()["code"], PROJECT_CODE)
        self.assertTrue(project_info()["title"])


if __name__ == "__main__":
    unittest.main()
