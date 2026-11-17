"use client";

import { useId, useState, type FormEvent, type ReactNode } from "react";

import { Button } from "@/components/button";
import { ConfirmDialog } from "@/components/dashboard/confirm-dialog";
import { getErrorMessage } from "@/lib/api/client";
import { PROJECT_NAME_MAX_LENGTH, type Project } from "@/lib/api/projects";

/** The id of the "new project" input, so quick-start links can focus it. */
export const NEW_PROJECT_INPUT_ID = "new-project-name";

export interface ProjectPanelProps {
  /** `null` while loading. */
  projects: readonly Project[] | null;
  total: number;
  offset: number;
  pageSize: number;
  error: string | null;
  selectedFilter: string;
  onRetry: () => void;
  onPage: (offset: number) => void;
  onSelect: (filter: string) => void;
  onCreate: (name: string) => Promise<void>;
  onRename: (project: Project, name: string) => Promise<void>;
  onArchive: (project: Project) => Promise<void>;
}

/** Returns an error message, or `null` when the name may be sent. */
export function validateProjectName(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) {
    return "Enter a project name.";
  }
  if (trimmed.length > PROJECT_NAME_MAX_LENGTH) {
    return `Use at most ${PROJECT_NAME_MAX_LENGTH} characters.`;
  }
  return null;
}

function countsText(project: Project): string {
  const { uploaded, processing, failed } = project.status_counts;
  const parts = [`${project.video_count} ${project.video_count === 1 ? "video" : "videos"}`];
  if (uploaded + processing > 0) {
    parts.push(`${uploaded + processing} processing`);
  }
  if (failed > 0) {
    parts.push(`${failed} failed`);
  }
  return parts.join(" · ");
}

/** Creates, lists, renames and archives the user's active projects. */
export function ProjectPanel({
  projects,
  total,
  offset,
  pageSize,
  error,
  selectedFilter,
  onRetry,
  onPage,
  onSelect,
  onCreate,
  onRename,
  onArchive,
}: ProjectPanelProps) {
  const headingId = useId();
  const createErrorId = useId();
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [savingRename, setSavingRename] = useState(false);
  const [pendingArchive, setPendingArchive] = useState<Project | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validateProjectName(name);
    setCreateError(problem);
    if (problem || creating) {
      return;
    }
    setCreating(true);
    try {
      await onCreate(name.trim());
      setName("");
    } catch (caught) {
      setCreateError(getErrorMessage(caught));
    } finally {
      setCreating(false);
    }
  }

  async function handleRename(event: FormEvent<HTMLFormElement>, project: Project) {
    event.preventDefault();
    if (!renaming) {
      return;
    }
    const problem = validateProjectName(renaming.name);
    setRenameError(problem);
    if (problem || savingRename) {
      return;
    }
    setSavingRename(true);
    try {
      await onRename(project, renaming.name.trim());
      setRenaming(null);
    } catch (caught) {
      setRenameError(getErrorMessage(caught));
    } finally {
      setSavingRename(false);
    }
  }

  async function confirmArchive() {
    const project = pendingArchive;
    if (!project || archiving) {
      return;
    }
    setArchiving(true);
    setActionError(null);
    try {
      await onArchive(project);
    } catch (caught) {
      setActionError(`Could not archive ${project.name}. ${getErrorMessage(caught)}`);
    } finally {
      setArchiving(false);
      setPendingArchive(null);
    }
  }

  let list: ReactNode;
  if (projects === null && error) {
    list = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm text-fg">
          Could not load your projects. {error}
        </p>
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  } else if (projects === null) {
    list = (
      <p role="status" className="text-sm text-fg-muted">
        Loading projects…
      </p>
    );
  } else if (projects.length === 0) {
    list = (
      <p className="text-sm text-fg-muted">
        No projects yet. Videos that are in no project are listed as unassigned.
      </p>
    );
  } else {
    list = (
      <ul className="flex flex-col gap-2">
        {projects.map((project) => {
          const isRenaming = renaming?.id === project.id;
          const renameInputId = `${headingId}-rename-${project.id}`;
          return (
            <li key={project.id} className="flex flex-col gap-2 rounded-md border border-border bg-surface p-3">
              {isRenaming ? (
                <form onSubmit={(event) => void handleRename(event, project)} className="flex flex-col gap-2">
                  <label htmlFor={renameInputId} className="text-sm font-medium text-fg">
                    New name for {project.name}
                  </label>
                  <input
                    id={renameInputId}
                    value={renaming.name}
                    maxLength={PROJECT_NAME_MAX_LENGTH}
                    onChange={(event) => setRenaming({ id: project.id, name: event.target.value })}
                    aria-invalid={renameError ? true : undefined}
                    className="rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
                  />
                  {renameError ? (
                    <p role="alert" className="text-sm text-fg">
                      {renameError}
                    </p>
                  ) : null}
                  <div className="flex gap-2">
                    <Button type="submit" disabled={savingRename}>
                      Save name
                    </Button>
                    <Button variant="secondary" onClick={() => setRenaming(null)} disabled={savingRename}>
                      Cancel
                    </Button>
                  </div>
                </form>
              ) : (
                <>
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium text-fg">{project.name}</span>
                    <span className="text-xs text-fg-muted">{countsText(project)}</span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="secondary"
                      aria-pressed={selectedFilter === project.id}
                      onClick={() => onSelect(selectedFilter === project.id ? "all" : project.id)}
                      aria-label={`Show videos in ${project.name}`}
                    >
                      Show videos
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => {
                        setRenameError(null);
                        setRenaming({ id: project.id, name: project.name });
                      }}
                      aria-label={`Rename ${project.name}`}
                    >
                      Rename
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => setPendingArchive(project)}
                      aria-label={`Archive ${project.name}`}
                    >
                      Archive
                    </Button>
                  </div>
                </>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <h2 id={headingId} className="text-xl font-semibold text-fg">
        Projects
      </h2>

      <form onSubmit={(event) => void handleCreate(event)} className="flex flex-col gap-2" noValidate>
        <label htmlFor={NEW_PROJECT_INPUT_ID} className="text-sm font-medium text-fg">
          New project name
        </label>
        <div className="flex gap-2">
          <input
            id={NEW_PROJECT_INPUT_ID}
            value={name}
            maxLength={PROJECT_NAME_MAX_LENGTH}
            onChange={(event) => setName(event.target.value)}
            aria-invalid={createError ? true : undefined}
            aria-describedby={createErrorId}
            className="min-w-0 flex-1 rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
          />
          <Button type="submit" disabled={creating}>
            {creating ? "Creating…" : "Create project"}
          </Button>
        </div>
        <div id={createErrorId} role="alert">
          {createError ? <p className="text-sm text-fg">{createError}</p> : null}
        </div>
      </form>

      <div role="alert">{actionError ? <p className="text-sm text-fg">{actionError}</p> : null}</div>

      {list}

      {projects && total > pageSize ? (
        <nav aria-label="Project pages" className="flex items-center justify-between gap-2">
          <Button variant="secondary" onClick={() => onPage(offset - pageSize)} disabled={offset === 0}>
            Previous
          </Button>
          <Button
            variant="secondary"
            onClick={() => onPage(offset + pageSize)}
            disabled={offset + pageSize >= total}
          >
            More projects
          </Button>
        </nav>
      ) : null}

      {pendingArchive ? (
        <ConfirmDialog
          title={`Archive ${pendingArchive.name}?`}
          confirmLabel="Archive project"
          busyLabel="Archiving…"
          busy={archiving}
          onConfirm={() => void confirmArchive()}
          onCancel={() => setPendingArchive(null)}
        >
          Its videos stay where they are, but no more videos can be moved into it.
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
