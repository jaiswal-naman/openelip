# Contributing to OpenElip

Thank you for helping. OpenElip is a one-maintainer project, so small, well-tested changes are the easiest to review and merge. For anything bigger than a fix, open an issue first and say what you want to change and why.

## Run it

```bash
git clone https://github.com/jaiswal-naman/openelip.git
cd openelip
cp .env.example .env
docker compose up -d          # http://localhost:8080
```

The first start builds two images, so it takes a few minutes. [README.md](README.md) explains first run, model providers and troubleshooting.

## Run the tests

You need Docker, [uv](https://docs.astral.sh/uv/) (Python 3.11), Node 22 and pnpm.

```bash
# Backend. The tests need the database.
docker compose up -d db
cd backend
uv sync --frozen
uv run pytest tests -q                       # or one folder, for example tests/match
uv run ruff check app tests ../db
uv run ruff format --check app tests ../db
uv run mypy app
uv run lint-imports                          # modules import only downward

# Web
cd web
pnpm install
pnpm typecheck && pnpm lint && pnpm test --run
```

Backend tests run on a database whose name ends in `_test` (`app_test` on `127.0.0.1:5433` by default; set `TEST_DATABASE_URL` to use another) and refuse any other name, so your own data is never touched. On Windows, `powershell -File scripts/ci-local.ps1` runs every gate of the CI workflow and prints one PASS or FAIL line each.

[docs/TESTING.md](docs/TESTING.md) explains each layer, how to run the browser tests (use a throwaway stack: they create accounts) and the eval suites, and which tests cover which feature.

## What a pull request needs

- **One purpose, small.** Several small pull requests are easier to review than one large one.
- **Tests that fail without your change.** A bug fix comes with a test that reproduces it. A feature comes with tests at the layer where it lives (see the matrix in `docs/TESTING.md`).
- **All gates green.** CI runs lint, format, types, import contracts, a migration check (an empty database up to head, down to the baseline, up again), the backend tests, one replay per eval suite, the web checks, the browser tests and a secret scan. Run the same locally first.
- **The API and the schema stay in step.** If you change a route or a response, regenerate `web/openapi.json` from the backend (`app.openapi()`) and `web/src/lib/api/schema.d.ts` with `pnpm gen:api`; CI fails when either is out of date. The database schema comes only from the migrations in `db/versions/`.
- **Docs move with behaviour.** If what a screen, an endpoint or a setting does changes, change the document that describes it (`docs/`, the README, `.env.example`). A few tests read `docs/DATA_MODEL.md`, `docs/AI_SYSTEM.md` and the suites' READMEs, so run the backend tests after editing them.
- **Match the code around you.** Plain, well-named code and small modules; modules import only downward (CI checks it). No new dependency without a reason, and pin its version.
- **Keep the invariants.** The eight in [docs/OVERVIEW.md](docs/OVERVIEW.md) are never traded away: a hard constraint is never shown as a match, every claim resolves to its source, a resume edit cannot add experience the record lacks, nothing about a candidate leaves without their action, no outside action runs unless both keys pass, nothing is sent twice, deletion is complete, and no quality number is published unless a recorded eval run produced it.
- **Honest numbers.** A replay of recorded answers is a wiring check, never a measure of model quality. Do not write an accuracy figure anywhere unless a recorded run produced it, and say which run.
- **Public sources only.** Read only public, documented job-board endpoints at a low request rate. No scraping behind a sign-in, and no collecting people's profiles. Do not copy another company's code, assets or branding.

## No secrets, no real people's data

- Never commit an API key, a token, a `.env` file, a resume or anything personal. The keys in `.env.example` are development-only and public by design; nothing real goes in that file.
- Test data, gold sets and demo data are **invented** and labelled so (invented addresses end in `.example` or `example.test`). Do not add real people's data.
- CI runs gitleaks on every pull request. If you think you committed a secret, rotate it first, then tell us.

To report a security problem, do not open a public issue; follow [SECURITY.md](SECURITY.md).

## Conduct

Be respectful and constructive. Harassment, personal attacks and discrimination are not tolerated here. Maintainers may edit or remove comments and contributions that break this and may block people who keep doing so.

## Licence

By contributing you agree that your contribution is licensed under the [MIT licence](LICENSE) of this project.
