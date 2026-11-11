"""Offline regression tests. Live evidence is recorded separately."""
import json
import subprocess
import unittest
from unittest.mock import patch
import fedwatch_secure as fw
import fetch_phase1 as phase1


def forecast(reporting, probability=0.9, meeting="2026-12-09"):
    return {"payload": [{"meetingDt": meeting, "reportingDt": reporting,
                         "rateRange": [
                             {"lowerRt": 400, "upperRt": 425, "probability": probability},
                             {"lowerRt": 425, "upperRt": 450, "probability": 1-probability}]}]}


def snapshot(reporting="2026-11-10", probability=0.9):
    return {"meetings_raw": {"payload": [{"meetingDt": "2026-12-09"}]},
            "forecasts": [forecast(reporting, probability)]}


class SecureTests(unittest.TestCase):
    def test_auth_wrapper_only_wraps_token_exchange(self):
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps({"access_token": "synthetic-test-token"}), ""),
            subprocess.CompletedProcess([], 0, '{"payload":[]}\n200', ""),
        ]
        with patch.object(fw.subprocess, "run", side_effect=responses) as run:
            token = fw.obtain_token()
            fw.api_get("/meetings/future", token)
        first, second = run.call_args_list
        self.assertEqual(first.args[0][:3], ["pplx", "credentials", "run"])
        self.assertIn(fw.DEFAULT_HANDLE, first.args[0])
        self.assertEqual(second.args[0][0], "curl")
        self.assertNotIn(token, " ".join(second.args[0]))
        self.assertIn(token, second.kwargs["input"])
        self.assertNotIn("env", second.kwargs)  # No proxy-environment override.

    def test_token_error_does_not_echo_response_or_retry(self):
        response = subprocess.CompletedProcess([], 22, "synthetic-secret-response", "synthetic-secret-error")
        with patch.object(fw.subprocess, "run", return_value=response) as run:
            with self.assertRaises(fw.FedWatchError) as error:
                fw.obtain_token()
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertEqual(run.call_count, 1)

    def test_data_error_is_sanitized(self):
        response = subprocess.CompletedProcess([], 7, "\n000", "synthetic-secret-error")
        with patch.object(fw.subprocess, "run", return_value=response):
            with self.assertRaises(fw.FedWatchError) as error:
                fw.api_get("/forecasts", "synthetic-test-token")
        self.assertNotIn("synthetic", str(error.exception))

    def test_live_requires_advanced_date_and_changed_fields(self):
        result = fw.validate_snapshot(snapshot(), forecast("2026-11-04", 0.8), "2026-11-10")
        self.assertEqual(result["status"], "LIVE")
        self.assertAlmostEqual(result["probability_sum"], 1)
        self.assertEqual(len(result["field_deltas"]), 2)

    def test_stale_date_rejected(self):
        with self.assertRaisesRegex(fw.FedWatchError, "did not advance"):
            fw.validate_snapshot(snapshot(), forecast("2026-11-10", 0.8), "2026-11-10")

    def test_identical_fields_rejected_even_if_date_advanced(self):
        with self.assertRaisesRegex(fw.FedWatchError, "Identical"):
            fw.validate_snapshot(snapshot(), forecast("2026-11-04"), "2026-11-10")

    def test_wrong_reporting_date_rejected(self):
        with self.assertRaisesRegex(fw.FedWatchError, "Unexpected reporting"):
            fw.validate_snapshot(snapshot(), forecast("2026-11-04", 0.8), "2026-11-11")

    def test_wrong_meeting_rejected(self):
        data = snapshot()
        data["forecasts"][0]["payload"][0]["meetingDt"] = "2027-01-27"
        with self.assertRaisesRegex(fw.FedWatchError, "do not match"):
            fw.validate_snapshot(data, forecast("2026-11-04", 0.8), "2026-11-10")

    def test_malformed_probabilities_rejected(self):
        for invalid in (float("nan"), -0.1, 2):
            data = snapshot()
            data["forecasts"][0]["payload"][0]["rateRange"][0]["probability"] = invalid
            with self.assertRaises(fw.FedWatchError):
                fw.validate_snapshot(data, forecast("2026-11-04", 0.8), "2026-11-10")

    def test_policy_band_has_no_stale_default(self):
        with self.assertRaises(TypeError):
            phase1._condense_forecast(forecast("2026-11-10"))
        result = phase1._condense_forecast(forecast("2026-11-10"), 400)
        self.assertEqual(result["hold"], 0.9)
        self.assertAlmostEqual(result["cumulative_hike"], 0.1)

    def test_missing_validation_context_does_not_fetch(self):
        with patch.dict(phase1.os.environ, {}, clear=True), patch.object(phase1, "fetch_snapshot") as fetch:
            self.assertEqual(phase1.fetch_fedwatch()["status"], "DEGRADED")
            fetch.assert_not_called()

    def test_phase1_uses_source_date_not_fetch_date(self):
        data = snapshot()
        data["fetched_at"] = "2026-11-11T14:41:35+00:00"
        with patch.object(phase1, "fetch_snapshot", return_value=data), \
             patch.object(phase1.Path, "read_text", return_value=json.dumps(forecast("2026-11-04", 0.8))):
            result = phase1.fetch_fedwatch(400, "2026-11-10", "/synthetic-prior.json")
        self.assertEqual(result["status"], "LIVE")
        self.assertEqual(result["as_of"], "2026-11-10")


if __name__ == "__main__":
    unittest.main()
