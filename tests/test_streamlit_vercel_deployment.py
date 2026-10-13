from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from app.services import vercel_deployment_service as service


class StreamlitVercelDeploymentTests(unittest.TestCase):
    def app(self, state=None):
        app = AppTest.from_string(
            "from frontend.vercel_deployment import render_vercel_deployment\n"
            "render_vercel_deployment(runtime_data_dir='test-runtime', server_name='demo', target='chatgpt-remote')\n",
            default_timeout=20,
        )
        if state is not None:
            app.session_state["vercel-wizard-test"] = state
        return app

    def setUp(self):
        for name, value in (("deployment_identity", "test"), ("read_deployment", {}), ("vercel_command", ["node", "vercel.js"])):
            patcher = patch.object(service, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ready_state(self):
        return {"account": {"username": "operator", "scopes": [{"slug": "demo-team", "name": "Demo"}]},
                "projects": {"scope": "demo-team", "projects": [{"id": "p1", "name": "existing"}]}, "deployment": {}}

    def test_first_screen_has_one_clear_next_step_and_no_deploy_action(self):
        app = self.app().run()
        self.assertFalse(app.exception)
        self.assertIn("Vercel 계정 연결", [button.label for button in app.button])
        self.assertNotIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])
        self.assertFalse(app.code)
        self.assertTrue(any("지금 할 일" in item.value for item in app.info))

    def test_missing_cli_points_to_install_instead_of_disabled_login(self):
        with patch.object(service, "vercel_command", side_effect=service.DeploymentError("도구 설치 필요")):
            app = self.app().run()
        self.assertFalse(app.exception)
        self.assertNotIn("Vercel 계정 연결", [button.label for button in app.button])
        self.assertFalse(next(button for button in app.button if button.label == "Vercel 배포 도구 설치").disabled)
        self.assertTrue(any("지금 할 일" in item.value for item in app.info))

    def test_public_checkbox_and_replacement_confirmation_gate_deploy(self):
        app = self.app(self.ready_state()).run()
        self.assertFalse(app.exception)
        deploy = next(button for button in app.button if button.label == "Vercel에 배포하고 연결 검사")
        self.assertTrue(deploy.disabled)
        app.checkbox[0].check().run()
        self.assertFalse(app.exception)
        self.assertFalse(next(button for button in app.button if button.label == deploy.label).disabled)
        app.radio[0].set_value("existing").run()
        self.assertTrue(next(button for button in app.button if button.label == deploy.label).disabled)
        app.checkbox[0].check().run()
        self.assertTrue(next(button for button in app.button if button.label == deploy.label).disabled)
        app.checkbox[1].check().run()
        self.assertFalse(next(button for button in app.button if button.label == deploy.label).disabled)

    def test_click_starts_only_one_background_job_and_disables_duplicate_publish(self):
        job = Mock(message="Vercel 업로드 중", login_url="", started=0)
        job.done.is_set.return_value = False
        with patch.object(service, "DeploymentJob", return_value=job) as start:
            app = self.app(self.ready_state()).run()
            app.checkbox[0].check().run()
            next(button for button in app.button if button.label == "Vercel에 배포하고 연결 검사").click().run()
            self.assertFalse(app.exception)
            start.assert_called_once()
            self.assertNotIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])

    def test_invalid_or_existing_new_project_name_cannot_publish(self):
        for name in ("invalid name", "--prod", "bad---name", "existing"):
            with self.subTest(name=name):
                app = self.app(self.ready_state()).run()
                app.text_input[0].set_value(name).run()
                app.checkbox[0].check().run()
                self.assertTrue(app.error)
                self.assertTrue(next(button for button in app.button if button.label == "Vercel에 배포하고 연결 검사").disabled)

    def test_previous_destination_is_selected_but_changed_bundle_is_not_complete(self):
        previous = {"identity": "older", "scope": "demo-team", "project": "existing",
                    "url": "https://existing.vercel.app/mcp", "verified": True}
        with patch.object(service, "read_previous_deployment", return_value=previous):
            app = self.app(self.ready_state()).run()
        self.assertFalse(app.exception)
        self.assertEqual("existing", app.radio[0].value)
        self.assertEqual("existing", app.selectbox[0].value)
        self.assertFalse(app.success)
        self.assertFalse(app.code)
        self.assertEqual(2, len(app.checkbox))
        self.assertTrue(all(not checkbox.value for checkbox in app.checkbox))

    def test_removed_projects_reset_old_widget_selection(self):
        app = self.app(self.ready_state()).run()
        app.radio[0].set_value("existing").run()
        state = app.session_state["vercel-wizard-test"]
        state["projects"] = {"scope": "demo-team", "projects": []}
        app.session_state["vercel-wizard-test"] = state
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual("new", app.radio[0].value)
        self.assertTrue(next(button for button in app.button if button.label == "Vercel에 배포하고 연결 검사").disabled)

    def test_expired_login_has_visible_reconnect_without_publishing_again(self):
        state = self.ready_state()
        state.update(error="로그인이 만료되었습니다.", error_code="login_required")
        app = self.app(state).run()
        self.assertFalse(app.exception)
        self.assertIn("Vercel 계정 다시 연결", [button.label for button in app.button])
        self.assertNotIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])

    def test_changed_account_cannot_keep_an_inaccessible_team_widget(self):
        state = self.ready_state()
        state["account"]["scopes"].append({"slug": "second-team", "name": "Second"})
        app = self.app(state)
        app.session_state["vercel-wizard-test-scope"] = "removed-team"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual("demo-team", app.selectbox[0].value)

    def test_account_is_reused_between_bundles(self):
        from frontend.vercel_deployment import ACCOUNT_STATE_KEY

        app = self.app()
        app.session_state[ACCOUNT_STATE_KEY] = self.ready_state()
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("Vercel 계정 연결", [button.label for button in app.button])
        self.assertIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])

    def test_failed_project_load_does_not_retry_forever(self):
        state = self.ready_state()
        state.update(projects={}, projects_failed=True, error="프로젝트 조회 실패")
        with patch.object(service, "DeploymentJob") as job:
            app = self.app(state).run()
        self.assertFalse(app.exception)
        job.assert_not_called()
        self.assertIn("프로젝트 목록 다시 불러오기", [button.label for button in app.button])

    def test_verified_receipt_shows_url_and_registration_instead_of_deploy_form(self):
        state = {"deployment": {"url": "https://demo.vercel.app/mcp", "scope": "team", "project": "demo", "verified": True}}
        app = self.app(state).run()
        self.assertFalse(app.exception)
        self.assertEqual("https://demo.vercel.app/mcp", app.code[0].value)
        self.assertTrue(any("다음은 AI 앱에 주소 등록" in item.value for item in app.success))
        self.assertNotIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])

    def test_failed_probe_keeps_url_and_retry_without_claiming_connection(self):
        state = {"deployment": {"url": "https://demo.vercel.app/mcp", "scope": "team", "project": "demo", "verified": False}}
        app = self.app(state).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.success)
        self.assertIn("연결 다시 검사", [button.label for button in app.button])
        self.assertEqual("https://demo.vercel.app/mcp", app.code[0].value)

    def test_expired_login_removes_cached_account_and_project_controls(self):
        job = Mock(error="로그인이 만료되었습니다.", result=None)
        job.done.is_set.return_value = True
        state = {**self.ready_state(), "job": job, "job_kind": "account"}
        app = self.app(state).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertFalse(app.selectbox)
        self.assertNotIn("Vercel에 배포하고 연결 검사", [button.label for button in app.button])


if __name__ == "__main__":
    unittest.main()
