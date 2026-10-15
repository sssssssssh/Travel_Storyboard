F                                                                        [100%]
=================================== FAILURES ===================================
____ test_current_readme_and_generated_analysis_package_use_release_version ____

client = <starlette.testclient.TestClient object at 0x10e297b60>

    def test_current_readme_and_generated_analysis_package_use_release_version(client):
        import zipfile
        from test_review_package_privacy import _seed_project
        from app.paths import resolve_in_project
        from app.services.review_package import _build_package
>       assert (ROOT/'README.md').read_text().splitlines()[0]=='# Travel Storyboard v2.1.2'
E       AssertionError: assert '# Travel Storyboard v2.0' == '# Travel Storyboard v2.1.2'
E
E         - # Travel Storyboard v2.1.2
E         ?                        ^^^
E         + # Travel Storyboard v2.0
E         ?                        ^

backend/tests/test_release_version.py:62: AssertionError
=========================== short test summary info ============================
FAILED backend/tests/test_release_version.py::test_current_readme_and_generated_analysis_package_use_release_version
