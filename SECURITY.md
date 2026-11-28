# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately through GitHub:

1. Go to the repository's **Security** tab: https://github.com/jaiswal-naman/openelip/security
2. Choose **Report a vulnerability** (GitHub's private vulnerability reporting, which opens a private security advisory visible only to you and the maintainer).

If you cannot see that option, open a public issue that says only that you need a private way to report a security problem, with no details, and the maintainer will arrange one.

Please include what you found, the steps to reproduce it, the version or commit you ran, and what an attacker could do with it. If you have a fix in mind, say so.

OpenElip is maintained by one person, so replies are best effort and no response time is promised. There is no bug bounty. Please give the maintainer a reasonable chance to fix the problem before you publish it. You will be credited in the advisory if you want to be.

## Supported versions

Fixes are made on `main`. There are no other supported branches and no published release images; to update an installed copy, pull `main` and run `docker compose up -d --build`.

## What counts

Worth reporting, for example:

- A way to see or change another person's data, or to bypass sign-in or a session.
- A way to make a job that breaks a hard constraint appear as a match, or to make the system send data about a candidate to an employer without that candidate's action.
- Text in a posting, a resume or a role description that makes the chat agent or a model task do something its controls should have stopped.
- A request that reaches a private address through the job-board fetchers or the apply-link check, or a secret, resume text or chat text that reaches a log, a metric or a trace.
- A weakness in how a model key is stored, or in the Docker images or the Compose files.

## What is by design

- The keys and passwords in `.env.example` are development-only and public. The server refuses them as soon as `APP_BASE_URL` is not a localhost address.
- Every published port is bound to `127.0.0.1`. The proxy speaks plain HTTP; putting TLS in front of a public server is not done for you.
- In development, sign-in codes are delivered to a local mail catcher (Mailpit) that anyone with access to your machine can read.
- What your model provider does with the text sent to it is governed by that provider's terms. The AI-processing consent shows the provider before anything identified is sent.
