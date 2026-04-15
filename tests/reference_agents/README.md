# Reference agents

This directory holds reference ADK agents used for demos (`make demo`,
`make demo-presentation`) and end-to-end tests under `tests/e2e/`. They
are illustrative samples — not production code — and exist to exercise
the `harmonograf_client` instrumentation against a real ADK runner.

Keep dependencies minimal: a reference agent should pull in ADK and
`harmonograf_client` and nothing else from this repo. Do not import
from `client/` internals or from `server/` runtime code; if a reference
agent needs a helper, the helper belongs in the public client API
first.
