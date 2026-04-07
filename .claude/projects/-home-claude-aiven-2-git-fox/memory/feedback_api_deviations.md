---
name: Keep API_DEVIATIONS.md updated
description: When any FoxESS API behaviour differs from docs or parsing logic changes, update API_DEVIATIONS.md alongside the code change.
type: feedback
---

API_DEVIATIONS.md documents differences between the official FoxESS Cloud API docs and actual observed behaviour. The user explicitly asked for this to be maintained alongside code changes.

**Why:** The official docs are unreliable — signature format, response shapes, and field semantics all deviate. This document is the ground truth for anyone using or extending this module.

**How to apply:** Whenever a code change in foxess/ adjusts API parsing, authentication, or endpoint usage because the real API differs from the docs, add or update the corresponding section in API_DEVIATIONS.md.
