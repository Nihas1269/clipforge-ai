# ClipForge AI — Frontend

The ClipForge AI web application, built with [Next.js](https://nextjs.org/)
(App Router), React, and TypeScript (`strict` mode). Styling uses
[Tailwind CSS](https://tailwindcss.com/); tests use
[Vitest](https://vitest.dev/) with
[React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)
and jsdom.

## Requirements

- **Node.js 24 (LTS)**. The version is pinned in [`.nvmrc`](.nvmrc), and the
  CI workflow (`.github/workflows/frontend.yml`) reads the same file. With
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

## Project structure

```text
src/
  app/          Routes (App Router): root layout, landing page, /login,
                /register, the /dashboard placeholder, and the shared
                loading, error, and not-found states
  components/   Reusable UI components (Button, Container, SiteHeader,
                StatusMessage, TextField) and the auth forms in auth/
  lib/          Shared constants, the API client (lib/api), and form
                validation (lib/validation)
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

Limitations: `/dashboard` is a placeholder that does not check the session
(the backend has no "current user" endpoint yet), so it does not redirect
logged-out visitors. The login and register pages do not redirect users who
are already logged in. There is no logout control in the UI yet; `logout()`
is available for it.
