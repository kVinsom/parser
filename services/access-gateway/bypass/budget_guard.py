"""Read the internal allowance before any gateway source operation."""

import json
import urllib.request
from urllib.error import HTTPError

from proxy_budget import CODES, BudgetPause


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BudgetGuard:
    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip("/")

    def check(self, kind):
        if kind not in {"catalog", "details"}:
            raise ValueError("Unknown budget lane")
        target = self.endpoint + "/proxy-budget?kind=" + kind + "&demand=true"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(target, timeout=2) as response:
                if response.status != 200 or response.geturl() != target:
                    raise ValueError("Budget status unavailable")
                raw = response.read(16385)
                if len(raw) > 16384:
                    raise ValueError("Oversized budget status")
                data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("Invalid budget status")
            code = data.get("code")
            if code == "ready":
                return
            if code in CODES:
                delay = data.get("retry_after")
                raise BudgetPause(code, delay if type(delay) is int and delay > 0 else 30)
            raise ValueError("Unrecognized budget status")
        except BudgetPause:
            raise
        except HTTPError as exc:
            exc.close()
            raise BudgetPause("ProxyBudgetUnavailable") from None
        except (OSError, ValueError, TypeError):
            raise BudgetPause("ProxyBudgetUnavailable") from None
