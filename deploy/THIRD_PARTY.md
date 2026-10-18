`seccomp-playwright.json` is vendored unchanged from Microsoft Playwright v1.63.0:
https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json

Apache License 2.0: see `LICENSE.playwright`.
It allows Chromium's user namespaces while retaining syscall filtering.
Reference: https://playwright.dev/python/docs/docker#crawling-and-scraping

Compose startup ordering follows Docker's health and completed-service conditions:
https://docs.docker.com/compose/how-tos/startup-order/
