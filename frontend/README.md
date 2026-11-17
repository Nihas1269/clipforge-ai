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
                /register, the protected /dashboard,
                /settings/profile, /videos/new, the /videos/[id] detail
                page, the /clips/[id] clip editor, and the shared
                loading, error, and not-found states
  components/   Reusable UI components (Button, Container, SiteHeader,
                StatusMessage, TextField), the auth forms in auth/, the
                session provider, protected layout and user menu in
                session/, the profile forms in profile/, the upload
                form and the video detail page in videos/, the
                dashboard in dashboard/, and the clip editor in clips/
  lib/          Shared constants, the API client (lib/api), media
                timestamps (lib/timestamps.ts), the clip editor's pure
                timing and validation helpers (lib/clip-editor.ts), and
                form validation (lib/validation)
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
- On success the page navigates to the video detail page, `/videos/{id}`.

Limitations: an abort that races the very end of the request body can still
let the backend store the video, and there is no endpoint yet to delete it.
Uploads are not resumable; a failed upload must be started again. Progress
measures bytes sent to the server, not the time the server then needs to
validate and store the file.

## Video detail page

`/videos/[id]` (`src/components/videos/video-detail.tsx`, protected by
`src/app/videos/layout.tsx`) shows one video:

- **Video.** A `<video>` player streaming `GET /api/v1/videos/{id}/media`
  (`videoMediaUrl`), the status badge, the processing error of a failed
  video, and its duration, resolution, frame rate, size, upload date and
  project (`getVideo` in `src/lib/api/videos.ts`). Another user's or a
  missing video shows "Video not found". While the video is `uploaded` or
  `processing` it is re-read every 4 s.
- **Transcript** (`transcript-panel.tsx`, `src/lib/api/transcripts.ts`).
  **Generate transcript** (with a **Language** select; "Detect
  automatically" sends no language) calls
  `POST /videos/{id}/transcription` and follows the job. The transcript is a
  scrollable list of segments with timestamps; clicking a segment seeks the
  player to its start. Segments without a usable timestamp are shown but
  cannot be clicked; an empty transcript says so. **Download SRT** /
  **Download VTT** link to `transcript.srt` / `transcript.vtt` (the session
  cookie authorizes the download), and **Refresh transcript** reloads it.
- **Clips** (`clips-panel.tsx`, `src/lib/api/clips.ts`). **Find clips** is
  enabled only when a transcript exists; it calls
  `POST /videos/{id}/clips/detect` and follows the job. Each clip shows its
  title, time range, duration, score, reason and status, with **Preview**,
  **Edit** (opens the clip editor, see below), **Accept** and **Reject** (`PATCH /clips/{id}` with `{status}`; the list
  shows the clip the backend returns). **Add clip manually** takes a start
  and end time (`m:ss`, `h:mm:ss` or seconds) and an optional title.
- **Jobs** (`src/lib/api/jobs.ts`, `use-tracked-job.ts`, `use-polling.ts`).
  A queued job is read with `GET /api/v1/jobs/{id}` every 2 s, only while it
  is `queued` or `running`; polling stops at `succeeded`, `failed` or
  `cancelled`, pauses while the tab is hidden (and checks again as soon as it
  is shown), never overlaps a pending request, and its timer and listener
  are removed when the page is left. A failed job shows its
  `error_message`. A `404`/`401`/`403`, or 3 failed checks in a row, stop
  polling with "Could not check the progress"; that is never shown as
  success.
- **Player control** (`player.ts`). Seeking and previews wait for the
  media's metadata (`loadedmetadata`), clamp times to the media's duration,
  and return a cleanup that removes their listeners. A preview seeks to the
  clip start, plays, and pauses at the clip end; it ends (and removes its
  listeners) when it reaches the end, when playback pauses or ends, when
  `play()` is refused, when another seek or preview starts, and on unmount.
- **Validation and pending actions.** Manual clips are checked in the
  browser (valid times, `start < end`, end within the video's duration)
  before any request; the backend checks the range and the allowed clip
  length again and its message is shown. Buttons are disabled while their
  request or job is pending, so actions cannot be sent twice.

Limitations: the backend has no endpoint that lists a video's active jobs,
so a job started before the page was opened (or before a reload) is not
followed; starting it again returns `409 transcription_in_progress` /
`clip_detection_in_progress`, which is shown. The clip length limits
(`CLIP_MIN_SECONDS`/`CLIP_MAX_SECONDS`) are not exposed to the browser, so
only the backend enforces them. Subtitle downloads are plain links: a
failure (for example an expired session) is shown by the browser, not the
page. Long transcripts are rendered in full inside a scrollable list (no
virtualization); the backend's duration limit keeps them bounded.

## Clip editor

`/clips/[id]` (`src/components/clips/clip-editor.tsx`, protected by
`src/app/clips/layout.tsx`) fine-tunes one clip before export. Each clip in
the video page's clip list has an **Edit** link to it. The page loads the
clip (`GET /clips/{id}`), its video, its captions, its caption style and the
clip length limits (`GET /clips/limits`); without the limits or the style
it still works (the backend checks the length on save; the overlay uses the
classic look).

- **Time bases.** Clip boundaries are seconds of the **source video** (the
  player's `currentTime`). Caption times are seconds **from the clip start**,
  as in the rendered clip. The overlay converts with
  `clip time = source time - clip start`. Moving the start therefore moves
  the captions with it (the backend does not shift them);
  **Regenerate from transcript** re-aligns them with the speech.
- **Preview.** The video plays only inside the clip: **Play clip** starts
  from the clip start when the playhead is outside the clip (the native play
  button also restarts from the clip start at the clip end), and playback
  stops at the clip end (or returns to the start when **Loop playback** is
  checked). While paused, the user can seek anywhere (native controls) to
  pick a new boundary. The preview follows the edited range as soon as it is
  ordered and inside the video.
- **Caption overlay.** An HTML overlay shows the captions for the current
  time with the ASS renderer's rules (`captionsAt` in
  `src/lib/clip-editor.ts`): times rounded to centiseconds, at least 1 cs
  long, shown while `start <= now < end`, text cleaned, upper-cased if the
  style says so and wrapped to `max_chars_per_line`. Font size is scaled
  from the 1080-pixel reference to the preview's shorter side, and the
  style's colors and position are used.
- **Boundaries.** Start and end inputs (seconds or `m:ss`), nudge buttons
  (±0.5 s and ±1 s) and **Set start/end to current time**. Rule: nudges and
  "current time" are clamped to the video (`0` to its duration) and rounded
  to milliseconds; order (`start < end`), the video's end and the clip
  length limits are never fixed silently, they are shown as errors and block
  saving.
- **Captions.** **Generate captions** (when there are none) or
  **Regenerate from transcript** (after a confirmation, because it replaces
  every caption) call `POST /clips/{id}/captions/generate`; generating is
  disabled while the boundaries have unsaved changes, because the backend
  generates for the saved range. Each line has its text and start/end (clip
  seconds) and can be removed; **Add caption** appends one after the last.
  The browser checks the backend's rules: non-blank text of at most 1000
  characters, lines within the style's `max_chars_per_line`,
  `0 <= start < end <= clip length`, in order and not overlapping, and at
  most 600 captions per clip.
- **Saving.** **Save changes** sends `PATCH /clips/{id}`
  (`{start_seconds, end_seconds}`) when the range changed, then
  `PUT /clips/{id}/captions` when the captions changed (captions are checked
  against the new length). Because the backend saves a range without
  checking the captions, a new range is also blocked while any caption,
  edited or not, would no longer fit in it; edit or remove those captions
  first. If the range fails, the captions are not sent.
  Backend errors are shown as returned, with the caption number for caption
  errors (`details.index`, or the `loc` of a `validation_error`).
  **Discard changes** returns to the last saved version.
- **Unsaved changes.** The header shows "Unsaved changes" or "All changes
  saved". Only while something really differs from the saved version
  (`10` and `10.0` are the same), the page asks before following a link
  (including Next.js links) and the browser asks before reloading or closing
  the tab.
- **Accessibility.** Every control is a native button, input, textarea or
  checkbox with a label; nudge buttons have descriptive names ("Move the
  start 0.5 seconds earlier"); errors are linked to their fields and
  announced.

Limitations: the browser Back/Forward buttons inside the app are not
intercepted (Next.js offers no way to cancel them), so only links, reloads
and closing the tab are guarded. The overlay previews on the source frame;
the export may be reframed, so caption position and size are approximate,
while the timing matches the rendered captions. Only the last saved version
can be restored (no revision history or undo/redo). The clip model has one
continuous source range, so there is no segment ordering; audio settings
(beyond muting the preview with the player's own controls), extra text
overlays and caption/branding presets other than the existing caption style
are not part of the clip or export model yet and are not offered.

## Dashboard

`/dashboard` (`src/components/dashboard/`) has three parts:

- **Overview** (`GET /api/v1/dashboard`): quick-start links (upload a video,
  create a project), video counts by status, generated clips, active
  projects, storage used and total video length, and the five most recent
  projects and videos.
- **Projects** (`src/lib/api/projects.ts`): create, rename and archive (after
  a confirmation dialog) projects; **Show videos** filters the video list to
  a project. The project list is loaded once by `Dashboard` and shared with
  the video list; the project shown for each video comes from the video
  response itself, so project data is not duplicated.
- **Your videos** (`listVideos`, `deleteVideo` and `moveVideoToProject` in
  `src/lib/api/videos.ts`): a paginated list (20 per page) with file name
  (linking to `/videos/{id}`), upload date, duration, size and a status badge
  whose text label ("Uploaded", "Processing", "Ready", "Failed") carries the
  meaning, plus status and project filters (including "Unassigned"), a
  per-video project selector and a delete action.

Behaviour:

- **Polling.** While any listed video is `uploaded` or `processing`, the
  current page is fetched again every 4 s with the same page and filters. A
  poll is never sent while a request is still pending; polling pauses while
  the browser tab is hidden (and refreshes as soon as it is shown again),
  stops once every listed video is `ready` or `failed`, and its timer is
  cleared on unmount.
- **Delete.** The confirmation is a modal `alertdialog`: focus starts on
  **Cancel**, Tab stays inside it, Escape cancels, and focus returns to the
  opener (or to the list heading after a successful deletion). A video is
  removed from the list only after the backend confirms; on failure it stays,
  the error is announced, and the list is reloaded from the backend.
- **States.** Each part has its own loading state, an error with a
  **Try again** button, and an empty state; the empty video list links to
  `/videos/new`.

Limitations: up to 50 active projects are loaded at a time; with more, the
project filter and selector only offer the current page of projects.
Credits are not tracked yet, so only usage is shown. There is no view of
archived projects (the API can list and restore them).
