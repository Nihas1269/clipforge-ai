# ClipForge AI — Agent Instructions

## Project overview

ClipForge AI is a SaaS application for automatically repurposing long-form
videos into short-form content.

The intended capabilities include:

- Video upload and storage
- Transcription and subtitle generation
- AI-assisted identification of interesting clips
- Vertical video reframing
- Caption customization
- Video preview and export
- User dashboard and subscription management

## Operating model: fully automatic

This repository is developed by an automatic agent pipeline. The repository
owner does not review or merge each pull request by hand. For every open
GitHub Issue labeled `agent:claude`, in issue-number order, the local agent
runner:

1. Creates a feature branch and worktree from the latest `main`.
2. Has Claude implement the issue.
3. Runs the project's automated checks (tests, builds, configuration
   validation) itself and records the real results.
4. Has a separate, read-only Claude reviewer review the change together with
   those real results; Claude fixes the findings and the checks run again.
5. Commits, pushes, opens a pull request, and merges it — but only when every
   merge gate below passes.
6. Syncs `main` and continues with the next issue.

Because no human checks each change, the automated checks are the safety net.
Treat them as the definition of "done".

## Merge gates

A pull request may be merged automatically only when **all** of these hold:

1. Every automated check that applies to the change was executed by the
   runner and passed. A check that could not run counts as not passed.
2. The Claude reviewer's final verdict is `APPROVED FOR HUMAN REVIEW` (the runner's
   approval token; no human review follows).
3. The committed files are exactly the files that were reviewed and tested.
4. The change contains no secrets, real credentials, or private keys.
5. The change does not modify `agent-runner/`, `AGENTS.md`, or anything that
   weakens or disables checks, branch protection, or the merge gates.

If any gate fails after the maximum number of fix attempts, the issue is
**blocked**: do not merge, label it `agent:blocked`, post the review report on
the issue, and stop. Later issues may depend on it, so the pipeline does not
skip ahead past a blocked issue.

## Working rules

1. Read the existing project files before making changes.
2. Follow the existing architecture and coding conventions.
3. Implement what the GitHub Issue asks for; do not invent extra
   requirements. Keep changes focused on the assigned issue.
4. Do not introduce unnecessary dependencies.
5. Write automated tests for meaningful behavior. Tests must not require
   network access, real accounts, or real credentials; use fakes, mocks, or
   local services instead.
6. Never fabricate, hardcode, or claim test or check results. Only results the
   runner actually executed count.
7. Explain important changes and any limitations in the pull request.

## When an issue needs accounts, keys, or other owner setup

Some work needs things only the repository owner can create: accounts with
external providers (AI/transcription APIs, payment providers, cloud storage,
email, hosting), API keys, webhook secrets, domains, or deployment targets.
Never guess, invent, or commit such values, and never ask the owner to paste a
secret into an issue, pull request, or comment.

Instead, keep going:

1. **Build everything that does not need the credential.** Read each value
   from configuration (environment variables via the settings module), add a
   placeholder such as `<your-razorpay-key-id>` to the matching `.env.example`,
   and make the feature fail clearly and safely (a clear error or a disabled
   feature, never a crash at startup) when the value is missing.
2. **Test it without the credential**, using fakes or mocks of the external
   service, so the merge gates can pass.
3. **Write a step-by-step setup guide** at `docs/setup/<service>.md` that a
   non-expert can follow, covering:
   - what the account or key is for and which features need it;
   - every step to create the account and obtain each value, including where
     to click in the provider's dashboard;
   - whether a free or test/sandbox mode exists, and to use it first;
   - exactly where to put each value (the local `.env` file, or GitHub
     repository secrets for CI/deployment) — never in committed files;
   - how to confirm it works.
4. **Flag it for the owner.** Add a section titled `## Owner setup required`
   to the pull request description, listing each needed value and linking the
   guide. The runner posts this on the issue and adds the `agent:owner-setup`
   label.
5. **Continue with the next issue.** Missing owner setup does not block the
   pipeline as long as the merge gates pass without it. Work that truly cannot
   be built or tested without the real credential is blocked as described
   above, with the guide posted on the issue.

## Git safety

1. Never commit directly to `main`; all changes reach `main` through a pull
   request from the issue's feature branch.
2. Work only on the feature branch assigned to the task.
3. Never force-push, rewrite published history, or delete remote branches
   other than an issue's own merged branch.
4. Pull requests are merged only by the runner, and only when every merge
   gate passes.

## Deployment

Agents may write deployment configuration (Dockerfiles, CI/CD workflows,
infrastructure files) when an issue asks for it, with secrets read from the
environment or GitHub secrets. Agents never run deployment commands or deploy
to production themselves. Deployment happens through CI/CD only after the
owner has completed the relevant `docs/setup/` guide.

## Agent responsibilities

### Claude

- Implement assigned GitHub Issues within the assigned feature branch.
- Write tests for the change.
- Fix review findings and failing checks.
- Follow the owner-setup procedure when credentials or accounts are needed.

### Claude reviewer

A separate Claude process with no tools and no shared session, so its review
is independent of the implementation.

- Review changes in read-only mode, together with the runner's real check
  results.
- Do not modify project files, commit, push, or merge.
- Report bugs, security concerns, missing tests, and other findings.
- Return `NEEDS CHANGES` if any check failed or did not run, if tests are
  missing for meaningful behavior, or if a change weakens security.

## Repository owner

The repository owner sets up accounts and keys using the guides in
`docs/setup/`, resolves `agent:blocked` issues, and can stop the pipeline at
any time by removing the `agent:claude` label or stopping the runner.
