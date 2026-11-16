# ClipForge AI — Frontend

The ClipForge AI web application, built with [Next.js](https://nextjs.org/)
(App Router), React, and TypeScript (`strict` mode). Styling uses
[Tailwind CSS](https://tailwindcss.com/); tests use
[Vitest](https://vitest.dev/) with
[React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)
and jsdom.

## Requirements

- **Node.js 24 (LTS)**. The version is pinned in [`.nvmrc`](.nvmrc), and the
  `frontend` job of the CI workflow (`.github/workflows/ci.yml`) reads the
  same file. With
  [nvm](https://github.com/nvm-sh/nvm) or
  [fnm](https://github.com/Schniz/fnm), run `nvm use` / `fnm use` in this
  directory.
- **npm** (bundled with Node.js). `package-lock.json` is the committed
  lockfile; do not use another package manager.

## Setup

Run all commands from the `frontend/` directory:

```bash
npm ci
```

`npm ci` installs the exact versions from `package-lock.json`. Use
`npm install <package>` only when adding or upgrading a dependency, and commit
the updated `package-lock.json`.

### Environment variables

| Variable                   | Default for local development | Purpose                       |
| -------------------------- | ----------------------------- | ----------------------------- |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000`       | Base URL of the backend API. |
| `NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB` | `500`                   | Fallback upload size limit (MiB), used only when the backend's limits cannot be loaded. Keep it equal to the backend's `MAX_UPLOAD_SIZE_MB`. |

[`.env.example`](.env.example) lists every variable with its local default.
To set them locally, copy it to `frontend/.env.local` (ignored by Git):

```bash
cp .env.example .env.local
```

Variables prefixed with `NEXT_PUBLIC_` are embedded in the browser bundle, so
never put secrets in them. When `NEXT_PUBLIC_API_BASE_URL` is unset, the
client falls back to `http://localhost:8000`.

### Connecting to the backend

The login session is the backend's HttpOnly `access_token` cookie
(`SameSite=Lax`). For the browser to store and send it:

- Open the frontend and the API on the **same host name**: use
  `http://localhost:3000` with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`.
  Mixing `localhost` and `127.0.0.1` makes them different sites and the cookie
  is not sent.
- List the frontend origin in the backend's `CORS_ORIGINS`
  (`CORS_ORIGINS=http://localhost:3000`, as in `backend/.env.example`). The
  backend also uses this list for its CSRF origin check, so requests from an
  unlisted origin get `403 csrf_failed`.

## Development

```bash
npm run dev
```

Opens the app at <http://localhost:3000>.

## Checks

| Command             | What it does                                      |
| ------------------- | ------------------------------------------------- |
| `npm run lint`      | Runs ESLint with the Next.js and TypeScript rules. |
| `npm run typecheck` | Type-checks the project with `tsc --noEmit`.       |
| `npm test`          | Runs the Vitest suite once.                        |
| `npm run test:watch`| Runs Vitest in watch mode.                         |
| `npm run build`     | Creates a production build in `.next/`.            |
| `npm start`         | Serves the production build (run `build` first).   |
| `npm audit --audit-level=high` | Fails on any high or critical dependency vulnerability. |

## Dependency security

The frontend uses Next.js 16, Vitest 5 and the flat ESLint configs shipped by
`eslint-config-next` 16. `npm audit --audit-level=high` must report no high or
critical vulnerabilities. When upgrading a dependency, keep the related
packages together (`next` with `eslint-config-next`; `react` with
`@types/react`). `@vitejs/plugin-react` 5.2 accepts Vite 4 to 8 as a peer, so
it works with the Vite version that Vitest 5 installs. Check its `vite` peer
range before moving it to a new major. Next.js 16 requires Node.js 20.9 or
newer, so the pinned Node.js 24 LTS is compatible.

Known remaining moderate vulnerabilities: none. `npm audit` reports
`found 0 vulnerabilities` (at any severity) for the current
`package-lock.json`. If a later audit reports a moderate vulnerability that has
no compatible fix, list it here with the reason it cannot be fixed yet.

Tests run with Vitest `globals: true` so that React Testing Library registers
its automatic `cleanup` after each test. Under Vitest 5, an `afterEach` call at
the top level of `vitest.setup.ts` fails with "Vitest failed to find the
current suite", so the setup file does not register hooks itself.

## Project structure

```text
src/
  app/          Routes (App Router): root layout, landing page, /login,
                /register, the protected /dashboard placeholder,
                /settings/profile, /videos/new and the /videos/[id]
                placeholder, and the shared loading, error, and
                not-found states
  components/   Reusable UI components (Button, Container, SiteHeader,
                StatusMessage, TextField), the auth forms in auth/, the
                session provider, protected layout and user menu in
                session/, the profile forms in profile/, and the upload
                form in videos/
  lib/          Shared constants, the API client (lib/api), and form
                validation (lib/validation)
  test-utils/   Test helpers (a fake XMLHttpRequest)
```

Tests sit next to the code they cover as `*.test.tsx`.

## UI foundation

- **Theme.** A single dark theme modeled on a video-editing workspace.
  Design tokens (colors, fonts) are defined once with Tailwind's `@theme` in
  `src/app/globals.css` and used as utilities such as `bg-surface`,
  `text-fg-muted`, and `border-border`.
- **States.** `StatusMessage` renders the loading, empty, error, and success
  states. Errors use `role="alert"`; the other states use `role="status"`.
- **Accessibility.** Pages include a skip link to the main content, controls
  have visible focus outlines, and the loading animation respects reduced
  motion preferences.

## API client and authentication

- `src/lib/api/client.ts` — `apiRequest` wraps `fetch` with the
  `NEXT_PUBLIC_API_BASE_URL` base, `credentials: "include"` on every request,
  JSON encoding and decoding, and a 15 s timeout. Non-2xx responses in the
  backend error shape `{error: {code, message, details}}` become a typed
  `ApiError` (`status`, `code`, `message`, `details`); other failures become an
  `ApiError` with a client-side code (`unknown_error`, `invalid_response`,
  `network_error`, or `timeout`, the last two with status `0`).
- `src/lib/api/auth.ts` — `register`, `login`, and `logout`. The session is
  the backend's HttpOnly cookie: the access token returned by login is
  discarded and never stored in `localStorage`, `sessionStorage`, URLs, or
  logs.
- `/register` and `/login` validate input in the browser with the backend's
  rules (an ASCII email address; a password of 8–256 characters). Field
  errors are linked to their inputs and announced politely; API errors are
  announced in an `aria-live="assertive"` alert; the submit button is
  disabled while a request is pending. Registration redirects to
  `/login?registered=1`, which shows a success message; login redirects to
  `/dashboard`.
- **401 handling.** The client never redirects; it throws an `ApiError` with
  status `401` and the caller decides what to show. This keeps `/login` free
  of redirect loops.

## Session and protected pages

- `src/lib/api/users.ts` — `getCurrentUser` (`GET /api/v1/users/me`),
  `updateProfile` (`PATCH /api/v1/users/me`, `full_name` only), and
  `changePassword` (`POST /api/v1/users/me/password`; the new token in the
  response is discarded, the backend replaces the cookie).
- `SessionProvider` (`src/components/session/session-provider.tsx`) wraps the
  app in the root layout. `useCurrentUser()` returns `user`, `loading`,
  `error` (a non-401 failure such as an unreachable API), `refresh`, `setUser`,
  and `clear`. Only the public user is kept in memory. Each `refresh()` starts
  a new request and only the latest response is applied, so a check started
  before login or logout can never overwrite the newer state.
- `AuthenticatedLayout` protects `/dashboard` and `/settings/*` through their
  `layout.tsx` files. It re-checks the session when it mounts and shows only a
  loading state until the check finishes, so protected content never flashes.
  A `401` redirects to `/login`; other failures show an error with a retry
  button instead of logging the user out.
- The header shows the user's name (or email, linking to `/settings/profile`)
  and a **Log out** button. Logout calls `POST /api/v1/auth/logout` (which
  clears the HttpOnly cookie), then clears the client session and redirects
  to `/login`. If the request fails, the user stays logged in and the error
  is shown, because the cookie may still be set.
- `/settings/profile` updates `full_name` (trimmed; blank clears it; at most
  255 characters) and changes the password (current password, plus a new
  password of 8–256 characters entered twice). Both forms announce success
  and backend errors, such as "The current password is incorrect.".

Requests are credentialed (`credentials: "include"`), so the frontend origin
must be listed in the backend's `CORS_ORIGINS` (see "Connecting to the
backend"); otherwise logout, profile changes and uploads get `403 csrf_failed`.

Limitations: the session check runs in the browser, so protected pages are
not checked during server rendering (the server only sends the loading
state; all data comes from the authenticated API). `/dashboard`,
`/settings` and `/videos` each have their own `layout.tsx` instead of a
shared `(app)` route group, so moving between them re-checks the session. The
login and register pages do not redirect users who are already logged in.

## Video upload

- `/videos/new` (protected by `src/app/videos/layout.tsx`) uploads one
  long-form video. Files can be chosen with the labelled **Video file** input
  or dropped on the drop zone, which is a native button (focusable, opened
  with Enter or Space) that opens the file picker. The dashboard links to it.
- **Limits.** The page loads `GET /api/v1/videos/upload-limits` (size in bytes
  and allowed extensions) so the browser uses the backend's own limits. Only
  if that request fails does it fall back to `NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB`
  (default 500) and `.mp4`, `.mov`, `.webm`, `.mkv`. Unsupported extensions,
  empty files and files over the limit are rejected without a request. These
  checks are only for the user's convenience; the backend validates every
  upload (type, file signature, size) and its errors are shown as returned.
- `src/lib/api/videos.ts` — `uploadVideo(file, {onProgress, onSent, signal})`
  posts `multipart/form-data` with the single field `file` to
  `POST /api/v1/videos` through `XMLHttpRequest` with `withCredentials`, so
  upload progress is available. If the browser reports no type for a file,
  the type is set from the extension (for example `.mkv` on some systems).
  It rejects with an `ApiError`: the backend error, `network_error`,
  `upload_cancelled`, `invalid_response` for a malformed success response,
  or `timeout` when no progress is reported for 60 s while sending, or no
  response arrives within 5 minutes after the file was sent.
  `getUploadLimits()` reads the limits.
- **Progress and announcements.** A `role="progressbar"` with
  `aria-valuenow`/`aria-valuetext` shows the percentage sent; state changes
  ("Uploading …", "Upload cancelled.", "Upload complete …") are announced
  politely and errors through an `aria-live="assertive"` alert. After a
  failure or cancel, focus returns to the drop zone.
- **Cancel.** **Cancel upload** (and leaving the page) aborts the request.
  The backend sees the disconnect, discards the partial body (`400
  upload_interrupted`) and stores nothing. Once the whole file has been sent
  the button is disabled, because the backend may already be storing it.
- On success the page navigates to `/videos/{id}`, a placeholder until the
  video detail page is built (CF-030).

Limitations: an abort that races the very end of the request body can still
let the backend store the video, and there is no endpoint yet to delete it.
Uploads are not resumable; a failed upload must be started again. Progress
measures bytes sent to the server, not the time the server then needs to
validate and store the file.
