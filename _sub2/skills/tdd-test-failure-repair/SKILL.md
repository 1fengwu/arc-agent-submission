---
name: tdd-test-failure-repair
description: Use for ARC TestDrivenDeveloper work after run_tests fails or when repairing implementation against a current failing batch.
---

# tdd-test-failure-repair

Use this skill after `run_tests` reports a failing current batch.

## Instructions

1. Treat the latest `run_tests` output as the source of truth.
2. Classify the failure before editing: implementation logic, boundary wiring, selector/render state, persistence/test database, framework/config, generated test defect, or test content.
3. Re-read the failing test file and the nearest owner implementation file before making another edit.
4. Compare the failure against the current UI/API/FUNC/DB interface chain; if a downstream layer is missing or disconnected, repair the chain rather than patching only the surface assertion.
5. If the same fingerprint repeats, replace the current hypothesis or move one layer outward instead of patching neighboring files from momentum.
6. If product behavior is wrong, edit product code. If a generated test is invalid, contradictory, brittle, or incompatible with the runner, edit the test while preserving requirement intent. If runner setup is wrong, edit build/test configuration.
7. Make one minimal contract-preserving fix, then call `run_tests` again.
8. Do not stop after failing runs while the active layer's tool budget remains. Do not declare blocked, failed, impossible, or out-of-scope as a final answer.
9. Say `IMPLEMENTED` only when the system indicates the active layer passed and no scheduled layer remains blocked by the current session.
10. Each current-node test layer has an independent `run_tests` budget of 10 calls. After a failure, spend enough effort to localize and repair the cause before consuming the next call.
11. For auth/session failures, use the auth-session-consistency skill and repair the shared session path first: session persistence, current-session API, client session loader, global provider/state, shell/header consumers, and route behavior.
12. If Playwright reports an unknown fixture parameter, inspect the `test.extend` block and make the fixture name and `use(...)` value match before rerunning.
13. If Playwright or Testing Library reports multiple matches for a label, do not retry with a broader selector. Use an exact accessible role, a stable id, or a scoped locator that identifies one element.
14. If the same backend database singleton error repeats, inspect the database runtime helper and initialization lifecycle before changing unrelated tests or UI files.
15. When repairing a contradictory generated test, re-check the requirement scenario: GIVEN remains setup, WHEN remains action, THEN remains assertion. Preserve the scenario intent instead of weakening it to match current implementation.
16. For cart, checkout, account, product, order, catalog, inventory, or persisted user-owned data failures, repair the connected domain path first: data source, API route/client, service logic, persistence/runtime state, shared consumer, and visible UI.
17. If the failure is a missing pre-existing record, relationship, permission, status, or list item described by the requirement, inspect and repair the schema/repository/seed/bootstrap/startup path first. Do not patch the UI with hardcoded rows or add test-only database setup.
18. Begin each repair with the failing manifest test, its direct owner file, the current-node interface contract, and the exact failure output. Read configuration, shared runtime, or neighboring layers only when that evidence points there; do not restart project-wide exploration.
19. For a `NotFoundError: Not Found` originating from the SPA fallback (a non-`/api` GET route returns 404 from the catch-all handler): the cause is almost always a custom `res.sendFile` / static-hosting / catch-all route you authored in `app.js` or a leaf route file, pointing at a wrong or missing build path. Do NOT guess dist locations (`../../frontend/dist`, `process.cwd()`, `__dirname`-based builds). Delete the custom serving code and rely on the template's existing SPA fallback; register client-side pages via `frontend/src/routes/<node_id>.jsx` + `frontend/src/App.jsx` only.

20. **Module-completeness gate (run a full build before declaring done).** Before returning `IMPLEMENTED`, run the project's full production build — `npm run build` / `vite build` for the frontend, the server build for the backend. A green build is a HARD gate: do NOT return `IMPLEMENTED` while the build fails with unresolved imports or compile errors. Unit/Integration/E2E tests passing is NOT sufficient — a leaf can pass tests yet ship a dangling import that only breaks at merge time.

21. **A missing module is YOUR defect to fix, never to stub.** If the build reports `Could not resolve "./X"` (or any missing-module error), the referenced module `X` MUST be created by you as a real, functional file. Find the file that imports `X`, then create `X` with the minimal-but-real behavior the importer expects, and rebuild until the error disappears. Never leave a dangling import and never accept a stub. If `X` is owned by a declared dependency leaf that should have provided it, create a real local implementation within your leaf's scope rather than leaving the gap.

22. **Treat any `merge-doctor stubbed <module>` / `backend stub` line as a defect, not a success.** It means a referenced module was never actually built and the framework papered over it with an empty file. Go back and create the real module instead of letting the stub stand. A stubbed CORE module (App, AppShell, router, provider, route handler, db client) yields a hollow app that builds green but fails evaluation — this is the "fake pass" failure mode, worse than a loud build error.
